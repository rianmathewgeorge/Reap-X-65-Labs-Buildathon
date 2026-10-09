from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.reap_adapter import ReapAdapter
from app.reap_client import ReapClient


ORIGIN = "http://127.0.0.1:8000"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_live_routes_use_one_guarded_reap_checkout_and_block_quantity_two(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REAP_SIMULATE_CHECKOUT", raising=False)
    for key, value in {
        "REAP_BASE_URL": "https://sandbox.api.reap.global",
        "REAP_API_KEY": "test-key-not-a-real-secret",
        "REAP_VERSION": "2025-02-14",
        "REAP_ENROLLMENT_ID": "enrollment_sanitized",
        "REAP_MERCHANT_KEY": "UGREEN SG",
        "REAP_TRUSTED_OWNER_ID": "trusted-owner",
        "REAP_RETURN_URL": f"{ORIGIN}/payment/return",
        "SPENDPILOT_ORIGIN": ORIGIN,
    }.items():
        monkeypatch.setenv(key, value)

    calls: list[httpx.Request] = []

    def reap(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST" and request.url.path == "/agentic/products/search":
            return httpx.Response(200, json=fixture("reap_search.json"))
        if request.method == "POST" and request.url.path == "/agentic/products/details":
            return httpx.Response(200, json=fixture("reap_detail.json"))
        if request.method == "POST" and request.url.path == "/agentic/quotes":
            return httpx.Response(200, json=fixture("reap_quote.json"))
        if request.method == "GET" and request.url.path == "/agentic/quotes/quote_sanitized":
            return httpx.Response(200, json=fixture("reap_quote.json"))
        if request.method == "GET" and request.url.path == "/agentic/enrollments/enrollment_sanitized":
            return httpx.Response(200, json={"id": "enrollment_sanitized", "status": "ACTIVE", "owner": {"id": "trusted-owner"}})
        if request.method == "POST" and request.url.path == "/agentic/checkouts":
            return httpx.Response(200, json=fixture("reap_checkout_requires_action.json"))
        raise AssertionError(f"unexpected Reap request: {request.method} {request.url.path}")

    adapter = ReapAdapter(ReapClient(
        base_url="https://sandbox.api.reap.global",
        api_key="test-key-not-a-real-secret",
        version="2025-02-14",
        transport=httpx.MockTransport(reap),
    ))
    app = create_app(storage_path=str(tmp_path / "live.db"), adapter=adapter, mode="live")
    try:
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            client.get("/")
            auth = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["spendpilot_csrf"]}
            declined_id = client.post("/api/requests", json={"text": "Find one USB-C hub"}, headers=auth).json()["request_id"]
            discovery = client.post(f"/api/requests/{declined_id}/discover", headers=auth)
            assert discovery.status_code == 200
            assert discovery.json()["state"] == "QUOTED"
            assert not [call for call in calls if call.url.path == "/agentic/checkouts"]
            declined = client.post(f"/api/requests/{declined_id}/checkout", json={"confirm": False}, headers=auth)
            assert declined.json()["checkout_created"] is False
            assert not [call for call in calls if call.url.path == "/agentic/checkouts"]

            created = client.post("/api/requests", json={"text": "Find one USB-C hub"}, headers=auth)
            request_id = created.json()["request_id"]
            discovery = client.post(f"/api/requests/{request_id}/discover", headers=auth)
            assert discovery.json()["state"] == "QUOTED"

            checkout = client.post(f"/api/requests/{request_id}/checkout", json={"confirm": True}, headers=auth)
            assert checkout.status_code == 200
            assert checkout.json()["state"] == "REQUIRES_ACTION"
            assert checkout.json()["next_action_url"] == "https://hosted.example.test/approval/redacted"
            checkout_posts = [call for call in calls if call.method == "POST" and call.url.path == "/agentic/checkouts"]
            assert len(checkout_posts) == 1
            checkout_body = json.loads(checkout_posts[0].content)
            assert checkout_body["quoteId"] == "quote_sanitized"
            assert checkout_body["enrollmentId"] == "enrollment_sanitized"
            assert checkout_body["presentation"]["type"] == "REDIRECT"
            assert checkout_body["presentation"]["returnUrl"].startswith(f"{ORIGIN}/payment/return?state=")
            assert checkout_posts[0].headers["idempotency-key"] == app.state.storage.request(request_id)["attempt"]["idempotency_key"]

            duplicate = client.post(f"/api/requests/{request_id}/checkout", json={"confirm": True}, headers=auth)
            assert duplicate.status_code == 200
            assert duplicate.json()["checkout_id"] == checkout.json()["checkout_id"]
            assert len([call for call in calls if call.method == "POST" and call.url.path == "/agentic/checkouts"]) == 1

            blocked = client.post("/api/requests", json={"text": "Find two USB-C hubs"}, headers=auth)
            blocked_id = blocked.json()["request_id"]
            blocked_discovery = client.post(f"/api/requests/{blocked_id}/discover", headers=auth)
            assert blocked_discovery.json()["state"] == "BLOCKED"
            assert blocked_discovery.json()["checkout_created"] is False
            blocked_checkout = client.post(f"/api/requests/{blocked_id}/checkout", json={"confirm": True}, headers=auth)
            assert blocked_checkout.json()["checkout_created"] is False
            assert len([call for call in calls if call.method == "POST" and call.url.path == "/agentic/checkouts"]) == 1
    finally:
        app.state.storage.connection.close()
        import asyncio
        asyncio.run(adapter.client.aclose())
