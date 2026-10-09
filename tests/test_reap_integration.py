from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.reap_adapter import ReapAdapter
from app.reap_client import ReapClient


ORIGIN = "http://127.0.0.1:8000"
CALLBACK = "https://relay.example.test/payment/return"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("return_url", [
    "http://relay.example.test/payment/return",
    "https://relay.example.test/not-payment-return",
    "https://relay.example.test/payment/return?state=provider",
    "https://user@relay.example.test/payment/return",
    "https://relay.example.test/payment/return#fragment",
])
def test_live_config_rejects_unclean_public_callback(tmp_path, monkeypatch, return_url):
    monkeypatch.setenv("SPENDPILOT_ORIGIN", ORIGIN)
    monkeypatch.setenv("REAP_RETURN_URL", return_url)
    with pytest.raises(RuntimeError, match="clean HTTPS"):
        create_app(storage_path=str(tmp_path / "live.db"), adapter=object(), mode="live")


def test_live_without_adapter_allows_a_future_clean_public_callback(tmp_path, monkeypatch):
    for key in ("REAP_API_KEY", "REAP_BASE_URL", "REAP_VERSION", "REAP_ENROLLMENT_ID", "REAP_MERCHANT_KEY", "REAP_TRUSTED_OWNER_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SPENDPILOT_ORIGIN", ORIGIN)
    monkeypatch.setenv("REAP_RETURN_URL", CALLBACK)
    app = create_app(storage_path=str(tmp_path / "live.db"), mode="live")
    try:
        assert app.state.checkout.return_url == CALLBACK
        assert app.state.adapter is None
    finally:
        app.state.storage.connection.close()


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
        "REAP_RETURN_URL": CALLBACK,
        "SPENDPILOT_ORIGIN": ORIGIN,
        "SPENDPILOT_SHIPPING_ADDRESS_JSON": json.dumps({
            "firstName": "Harbour",
            "lastName": "Studio",
            "phone": "+6590000000",
            "addressLine1": "1 Sandbox Way",
            "city": "Singapore",
            "country": "SG",
        }),
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
            search = next(call for call in calls if call.url.path == "/agentic/products/search")
            assert json.loads(search.content)["query"] == "UGREEN SG USB-C hub"
            quote = next(call for call in calls if call.url.path == "/agentic/quotes")
            assert json.loads(quote.content)["shippingAddress"]["country"] == "SG"
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
            assert checkout_body["presentation"]["returnUrl"].startswith(f"{CALLBACK}?state=")
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
            assert blocked_discovery.json()["rule_results"][0]["rule"] == "verified_discovery"
            blocked_checkout = client.post(f"/api/requests/{blocked_id}/checkout", json={"confirm": True}, headers=auth)
            assert blocked_checkout.json()["checkout_created"] is False
            assert len([call for call in calls if call.method == "POST" and call.url.path == "/agentic/checkouts"]) == 1
    finally:
        app.state.storage.connection.close()
        import asyncio
        asyncio.run(adapter.client.aclose())


def test_live_unknown_shipping_metadata_requires_trusted_address_before_quote(tmp_path, monkeypatch):
    for key, value in {
        "REAP_BASE_URL": "https://sandbox.api.reap.global",
        "REAP_API_KEY": "test-key-not-a-real-secret",
        "REAP_VERSION": "2025-02-14",
        "REAP_ENROLLMENT_ID": "enrollment_sanitized",
        "REAP_MERCHANT_KEY": "UGREEN SG",
        "REAP_TRUSTED_OWNER_ID": "trusted-owner",
        "REAP_RETURN_URL": CALLBACK,
        "SPENDPILOT_ORIGIN": ORIGIN,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SPENDPILOT_SHIPPING_ADDRESS_JSON", raising=False)

    calls: list[httpx.Request] = []

    def reap(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/agentic/enrollments/enrollment_sanitized":
            return httpx.Response(200, json={"id": "enrollment_sanitized", "status": "ACTIVE", "owner": {"id": "trusted-owner"}})
        if request.method == "POST" and request.url.path == "/agentic/products/search":
            return httpx.Response(200, json=fixture("reap_search.json"))
        if request.method == "POST" and request.url.path == "/agentic/products/details":
            return httpx.Response(200, json=fixture("reap_detail.json"))
        raise AssertionError(f"quote or checkout must not run: {request.method} {request.url.path}")

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
            request_id = client.post("/api/requests", json={"text": "Find one USB-C hub"}, headers=auth).json()["request_id"]
            discovery = client.post(f"/api/requests/{request_id}/discover", headers=auth).json()
            assert discovery["state"] == "BLOCKED"
            assert "shipping may be required" in discovery["rule_results"][0]["detail"]
            assert all(call.url.path != "/agentic/quotes" for call in calls)
    finally:
        app.state.storage.connection.close()
        import asyncio
        asyncio.run(adapter.client.aclose())


@pytest.mark.parametrize(("field", "expected"), [
    ("shippingAddress", "Reap rejected the configured shipping address; use organiser-provided sandbox shipping values before creating a new quote. No checkout was called."),
    ("shippingAddress.country", "Reap rejected the configured shipping address; use organiser-provided sandbox shipping values before creating a new quote. No checkout was called."),
    ("postalCode", "Reap discovery or trusted shipping configuration could not be verified"),
    (None, "Reap discovery or trusted shipping configuration could not be verified"),
])
def test_live_quote_rejection_keeps_provider_shipping_details_private(tmp_path, monkeypatch, field, expected):
    secret = "private-shipping-address-and-provider-detail"
    for key, value in {
        "REAP_BASE_URL": "https://sandbox.api.reap.global",
        "REAP_API_KEY": "test-key-not-a-real-secret",
        "REAP_VERSION": "2025-02-14",
        "REAP_ENROLLMENT_ID": "enrollment_sanitized",
        "REAP_MERCHANT_KEY": "UGREEN SG",
        "REAP_TRUSTED_OWNER_ID": "trusted-owner",
        "REAP_RETURN_URL": CALLBACK,
        "SPENDPILOT_ORIGIN": ORIGIN,
        "SPENDPILOT_SHIPPING_ADDRESS_JSON": json.dumps({
            "firstName": "Harbour",
            "lastName": "Studio",
            "phone": "+6590000000",
            "addressLine1": secret,
            "city": "Singapore",
            "country": "SG",
        }),
    }.items():
        monkeypatch.setenv(key, value)

    calls: list[httpx.Request] = []

    def reap(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/agentic/enrollments/enrollment_sanitized":
            return httpx.Response(200, json={"id": "enrollment_sanitized", "status": "ACTIVE", "owner": {"id": "trusted-owner"}})
        if request.method == "POST" and request.url.path == "/agentic/products/search":
            return httpx.Response(200, json=fixture("reap_search.json"))
        if request.method == "POST" and request.url.path == "/agentic/products/details":
            return httpx.Response(200, json=fixture("reap_detail.json"))
        if request.method == "POST" and request.url.path == "/agentic/quotes":
            return httpx.Response(400, json={"error": {
                "code": "AGENTIC_REQUEST_REJECTED",
                "message": secret,
                "detail": {"errors": [{"field": field, "message": secret}] if field is not None else [secret]},
            }})
        raise AssertionError(f"checkout must not run: {request.method} {request.url.path}")

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
            request_id = client.post("/api/requests", json={"text": "Find one USB-C hub"}, headers=auth).json()["request_id"]
            discovery = client.post(f"/api/requests/{request_id}/discover", headers=auth).json()
            assert discovery["state"] == "BLOCKED"
            assert discovery["rule_results"][0]["detail"] == expected
            assert secret not in json.dumps(discovery)
            assert secret not in client.get(f"/api/requests/{request_id}/passport").text
            assert app.state.storage.request(request_id)["attempt"] is None
            assert app.state.storage.budget_snapshot()["reserved_minor"] == 0
            assert not [call for call in calls if call.url.path == "/agentic/checkouts"]
    finally:
        app.state.storage.connection.close()
        import asyncio
        asyncio.run(adapter.client.aclose())


@pytest.mark.parametrize("enrollment", [
    {"id": "enrollment_sanitized", "status": "REQUIRES_ACTION", "owner": {"id": "trusted-owner"}},
    {"id": "enrollment_sanitized", "status": "ACTIVE", "owner": {"id": "different-owner"}},
])
def test_live_requires_an_active_matching_enrollment_before_catalog_or_checkout(tmp_path, monkeypatch, enrollment):
    for key, value in {
        "REAP_BASE_URL": "https://sandbox.api.reap.global",
        "REAP_API_KEY": "test-key-not-a-real-secret",
        "REAP_VERSION": "2025-02-14",
        "REAP_ENROLLMENT_ID": "enrollment_sanitized",
        "REAP_MERCHANT_KEY": "UGREEN SG",
        "REAP_TRUSTED_OWNER_ID": "trusted-owner",
        "REAP_RETURN_URL": CALLBACK,
        "SPENDPILOT_ORIGIN": ORIGIN,
    }.items():
        monkeypatch.setenv(key, value)

    calls: list[httpx.Request] = []

    def reap(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/agentic/enrollments/enrollment_sanitized":
            return httpx.Response(200, json=enrollment)
        raise AssertionError(f"catalog or checkout must not run: {request.method} {request.url.path}")

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
            assert client.get("/api/scope").json()["enrollment_ready"] is False
            request_id = client.post("/api/requests", json={"text": "Find one USB-C hub"}, headers=auth).json()["request_id"]
            discovery = client.post(f"/api/requests/{request_id}/discover", headers=auth).json()
            assert discovery["state"] == "BLOCKED"
            assert discovery["candidate"] is None and discovery["quote"] is None
            assert discovery["rule_results"][0]["rule"] == "active_trusted_enrollment"
            assert "ACTIVE Reap enrollment" in discovery["rule_results"][0]["detail"]
            blocked = client.post(f"/api/requests/{request_id}/checkout", json={"confirm": True}, headers=auth).json()
            assert blocked["checkout_created"] is False
            assert all(call.url.path == "/agentic/enrollments/enrollment_sanitized" for call in calls)
    finally:
        app.state.storage.connection.close()
        import asyncio
        asyncio.run(adapter.client.aclose())
