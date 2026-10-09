import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.agent import FakeReapAdapter
from app.main import create_app

ORIGIN = "http://127.0.0.1:8000"


def headers(client):
    client.get("/")
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("spendpilot_csrf")}


def quoted(client, auth, text="Find one approved USB-C hub"):
    created = client.post("/api/requests", json={"text": text}, headers=auth)
    assert created.status_code == 200
    request_id = created.json()["request_id"]
    response = client.post(f"/api/requests/{request_id}/discover", headers=auth)
    assert response.status_code == 200
    return request_id, response.json()


@pytest.fixture
def demo(tmp_path):
    adapter = FakeReapAdapter()
    app = create_app(storage_path=str(tmp_path / "demo.db"), adapter=adapter, mode="policy_test")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        yield app, adapter, client, headers(client)
    app.state.storage.connection.close()


def checkout(client, auth, request_id):
    response = client.post(f"/api/requests/{request_id}/checkout", json={"confirm": True}, headers=auth)
    assert response.status_code == 200
    return response.json()


def test_policy_test_outcomes_settle_or_release_the_existing_reservation(demo):
    app, adapter, client, auth = demo
    success_id, _ = quoted(client, auth)
    checkout(client, auth, success_id)
    success = client.post(f"/api/requests/{success_id}/policy-test-outcome", json={"outcome": "COMPLETED"}, headers=auth)
    assert success.status_code == 200
    assert success.json()["fixture_notice"] == "POLICY TEST - SIMULATED OUTCOME; NO REAL PAYMENT"
    assert success.json()["attempt"]["settlement_state"] == "SETTLED"
    assert success.json()["attempt"]["final_amount_minor"] == 7290
    assert app.state.storage.budget_snapshot()["completed_minor"] == 7290
    assert any(event["source"] == "POLICY_TEST" and event["event_type"] == "checkout_status_refreshed" for event in app.state.storage.events(success_id))

    failure_id, _ = quoted(client, auth)
    checkout(client, auth, failure_id)
    failure = client.post(f"/api/requests/{failure_id}/policy-test-outcome", json={"outcome": "FAILED"}, headers=auth)
    assert failure.status_code == 200
    assert failure.json()["attempt"]["settlement_state"] == "RELEASED"
    budget = app.state.storage.budget_snapshot()
    assert budget["reserved_minor"] == 0 and budget["completed_minor"] == 7290


def test_policy_test_outcome_rejects_invalid_missing_and_replayed_attempts(demo):
    app, adapter, client, auth = demo
    request_id = client.post("/api/requests", json={"text": "USB-C hub"}, headers=auth).json()["request_id"]
    assert client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "COMPLETED"}, headers=auth).status_code == 409
    request_id, _ = quoted(client, auth)
    checkout(client, auth, request_id)
    assert client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "UNKNOWN"}, headers=auth).status_code == 422
    assert client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "COMPLETED", "extra": True}, headers=auth).status_code == 422
    assert client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "COMPLETED"}, headers=auth).status_code == 200
    assert client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "FAILED"}, headers=auth).status_code == 409


def test_policy_test_outcomes_are_unavailable_for_live_or_nonfixture_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("REAP_RETURN_URL", "https://demo.invalid/payment/return")
    for mode, adapter in (("policy_test", object()), ("live", object())):
        app = create_app(storage_path=str(tmp_path / f"{mode}.db"), adapter=adapter, mode=mode)
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            auth = headers(client)
            request_id = client.post("/api/requests", json={"text": "USB-C hub"}, headers=auth).json()["request_id"]
            response = client.post(f"/api/requests/{request_id}/policy-test-outcome", json={"outcome": "COMPLETED"}, headers=auth)
            assert response.status_code == 404
            assert app.state.storage.request(request_id)["attempt"] is None
        app.state.storage.connection.close()


def test_allowed_and_blocked_passports(demo):
    app, adapter, client, auth = demo
    request_id, discovery = quoted(client, auth)
    assert all(rule["outcome"] == "PASS" for rule in discovery["rule_results"])
    result = checkout(client, auth, request_id)
    assert result["state"] == "REQUIRES_ACTION"
    assert app.state.storage.budget_snapshot()["reserved_minor"] == 7290
    blocked_id, blocked = quoted(client, auth, "Find two USB-C hubs")
    assert blocked["state"] == "BLOCKED" and blocked["checkout_created"] is False
    assert checkout(client, auth, blocked_id)["checkout_created"] is False
    assert adapter.create_checkout_calls == 1
    passport = client.get(f"/api/requests/{blocked_id}/passport").json()
    assert passport["state"] == "BLOCKED"
    assert {"USER_SCOPE", "POLICY_TEST", "APP_POLICY"} <= {event["source"] for event in passport["events"]}


@pytest.mark.parametrize("change", ["over_budget", "merchant"])
def test_backend_policy_block_has_zero_checkout_posts(demo, change):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    record = app.state.storage.request(request_id)
    if change == "over_budget":
        record["quote"]["total_minor"] = 10001
        adapter.quotes[record["quote_id"]]["total_minor"] = 10001
    else:
        record["candidate"]["merchant_key"] = "unapproved"
    app.state.storage.save_discovery(request_id, record["candidate"], record["quote"], "policy_test")
    result = checkout(client, auth, request_id)
    assert result["state"] == "BLOCKED" and result["checkout_created"] is False
    assert adapter.create_checkout_calls == 0


def test_reservation_exists_before_post_and_concurrent_clicks_share_attempt(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    original = adapter.create_checkout

    async def slow_post(*args):
        attempt = app.state.storage.request(request_id)["attempt"]
        assert attempt["settlement_state"] == "HELD"
        assert not app.state.storage.connection.in_transaction
        assert json.loads(attempt["request_body_json"])["return_url"] == args[2]
        await asyncio.sleep(0.01)
        return await original(*args)

    adapter.create_checkout = slow_post

    async def concurrent():
        return await asyncio.gather(*(app.state.checkout.guarded_create_checkout(request_id, True) for _ in range(2)))

    asyncio.run(concurrent())
    checkout(client, auth, request_id)
    client.get(f"/api/requests/{request_id}")
    assert adapter.create_checkout_calls == 1
    assert app.state.storage.connection.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 1


def test_run_budget_atomic_across_requests(demo):
    app, adapter, client, auth = demo
    ids = [quoted(client, auth)[0] for _ in range(3)]

    async def concurrent():
        return await asyncio.gather(*(app.state.checkout.guarded_create_checkout(request_id, True) for request_id in ids))

    results = asyncio.run(concurrent())
    assert sum(result["blocked"] for result in results) == 1
    assert adapter.create_checkout_calls == 2
    assert app.state.storage.budget_snapshot()["available_minor"] == 5420


def test_timeout_retains_key_and_reservation_without_false_no_checkout_claim(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)

    async def timeout(*args):
        adapter.create_checkout_calls += 1
        raise TimeoutError("secret remote body must never be shown")

    adapter.create_checkout = timeout
    first = checkout(client, auth, request_id)
    key = app.state.storage.request(request_id)["attempt"]["idempotency_key"]
    second = checkout(client, auth, request_id)
    assert first["state"] == second["state"] == "UNKNOWN"
    assert first["checkout_created"] is None
    assert adapter.create_checkout_calls == 1
    assert app.state.storage.request(request_id)["attempt"]["idempotency_key"] == key
    assert app.state.storage.budget_snapshot()["reserved_minor"] == 7290
    assert "secret remote body" not in client.get(f"/api/requests/{request_id}/passport").text


@pytest.mark.parametrize("status", ["FAILED", "EXPIRED", "COMPLETED"])
def test_terminal_reconciliation_once_and_no_regression(demo, status):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    result = checkout(client, auth, request_id)
    adapter.checkouts[result["checkout_id"]].update(status=status, final_amount_minor=7290 if status == "COMPLETED" else None, order_id="fixture-order" if status == "COMPLETED" else None)
    asyncio.run(app.state.checkout.reconcile(request_id))
    budget = app.state.storage.budget_snapshot()
    assert budget["reserved_minor"] == 0
    assert budget["completed_minor"] == (7290 if status == "COMPLETED" else 0)
    adapter.checkouts[result["checkout_id"]]["status"] = "PROCESSING"
    asyncio.run(app.state.checkout.reconcile(request_id))
    assert app.state.storage.request(request_id)["state"] == status
    assert app.state.storage.budget_snapshot() == budget


def test_missing_actual_amount_holds_completed_reservation(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    result = checkout(client, auth, request_id)
    adapter.checkouts[result["checkout_id"]]["status"] = "COMPLETED"
    asyncio.run(app.state.checkout.reconcile(request_id))
    record = app.state.storage.request(request_id)
    assert record["state"] == "COMPLETED"
    assert record["attempt"]["settlement_state"] == "HELD"
    assert app.state.storage.budget_snapshot()["reserved_minor"] == 7290
    assert app.state.storage.scope()["suspended"]


def test_settlement_discrepancy_suspends_new_purchases(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    result = checkout(client, auth, request_id)
    adapter.checkouts[result["checkout_id"]].update(status="COMPLETED", final_amount_minor=10001)
    asyncio.run(app.state.checkout.reconcile(request_id))
    assert app.state.storage.budget_snapshot()["completed_minor"] == 10001
    assert app.state.storage.scope()["suspended"]
    next_id, _ = quoted(client, auth)
    assert checkout(client, auth, next_id)["state"] == "BLOCKED"
    assert adapter.create_checkout_calls == 1


def test_changed_identity_and_malformed_failure_never_release(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    result = checkout(client, auth, request_id)
    adapter.checkouts[result["checkout_id"]].update(status="FAILED", checkout_id="contradictory")
    asyncio.run(app.state.checkout.reconcile(request_id))
    record = app.state.storage.request(request_id)
    assert record["attempt"]["checkout_id"] == result["checkout_id"]
    assert record["state"] == "UNKNOWN"
    assert app.state.storage.budget_snapshot()["reserved_minor"] == 7290


def test_quote_change_requires_review_and_no_post(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    record = app.state.storage.request(request_id)
    adapter.quotes[record["quote_id"]]["total_minor"] = 7300
    result = checkout(client, auth, request_id)
    assert result["state"] == "REVIEW_REQUIRED"
    assert client.get(f"/api/requests/{request_id}").json()["state"] == "REVIEW_REQUIRED"
    assert adapter.create_checkout_calls == 0


def test_return_uses_persisted_state_and_reap_status(demo):
    app, adapter, client, auth = demo
    request_id, _ = quoted(client, auth)
    result = checkout(client, auth, request_id)
    body = json.loads(app.state.storage.request(request_id)["attempt"]["request_body_json"])
    state = parse_qs(urlsplit(body["return_url"]).query)["state"][0]
    assert client.get("/payment/return?state=made-up&status=COMPLETED").status_code == 404
    response = client.get("/payment/return", params={"state": state, "status": "COMPLETED"})
    assert response.status_code == 200
    assert app.state.storage.request(request_id)["state"] == "REQUIRES_ACTION"
    assert adapter.create_checkout_calls == 1
    adapter.checkouts[result["checkout_id"]].update(status="COMPLETED", final_amount_minor=7290)
    client.get("/payment/return", params={"state": state})
    assert app.state.storage.request(request_id)["state"] == "COMPLETED"


def test_passport_redaction_and_fixture_sources(demo):
    app, adapter, client, auth = demo
    original = adapter.get_product

    async def personal_raw(product_id):
        return {**await original(product_id), "raw_evidence": {"api_key": "super-secret", "email": "private@example.com", "card": "raw-card"}}

    adapter.get_product = personal_raw
    request_id, _ = quoted(client, auth)
    checkout(client, auth, request_id)
    passport = client.get(f"/api/requests/{request_id}/passport")
    text = passport.text
    assert all(value not in text for value in ("super-secret", "private@example.com", "raw-card", "idempotency_key", "next_action_url", "return_state"))
    assert "POLICY TEST" in text and "REAP_RESPONSE" not in text
    public = client.get(f"/api/requests/{request_id}").text
    assert "request_body" not in public and "return_state" not in public and "idempotency_key" not in public


def test_session_origin_host_and_input_guards(demo):
    app, adapter, client, auth = demo
    assert client.post("/api/requests", json={"text": "USB-C hub"}).status_code == 403
    assert client.post("/api/requests", json={"text": "USB-C hub"}, headers={**auth, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/requests", json={"text": "USB-C hub", "budget": 999999}, headers=auth).status_code == 422
    request_id, _ = quoted(client, auth)
    assert client.post(f"/api/requests/{request_id}/checkout", json={"confirm": "true"}, headers=auth).status_code == 422
    assert client.get("/api/scope").headers["cache-control"] == "no-store"
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.1", 50000)) as remote:
        assert remote.get("/").status_code == 403


def test_live_no_adapter_never_falls_back_and_modes_isolate_data(tmp_path):
    live = create_app(storage_path=str(tmp_path / "live.db"), mode="live")
    assert live.state.adapter is None
    with TestClient(live, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        auth = headers(client)
        request_id = client.post("/api/requests", json={"text": "USB-C hub"}, headers=auth).json()["request_id"]
        assert client.post(f"/api/requests/{request_id}/discover", headers=auth).status_code == 503
    with pytest.raises(RuntimeError):
        create_app(storage_path=str(tmp_path / "live.db"), mode="policy_test")
    with pytest.raises(RuntimeError):
        create_app(storage_path=str(tmp_path / "fake.db"), mode="live", adapter=FakeReapAdapter())


def test_agent_optional_model_is_one_strict_call_and_excludes_private_prose(monkeypatch):
    import httpx
    from app.agent import _interpret_request

    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            return httpx.Response(200, request=httpx.Request("POST", url), json={"output_text": '{"query":"USB-C hub","quantity":1}'})

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setattr("app.agent.httpx.AsyncClient", Client)
    events = []
    query = asyncio.run(_interpret_request("Find one USB-C hub; private@example.com, enrollment enr_private, 4111111111111111", {"permitted_quantity": 1}, events))
    assert query == "USB-C hub" and len(calls) == 1
    payload = json.dumps(calls[0])
    assert all(secret not in payload for secret in ("private@example.com", "enr_private", "4111111111111111", "synthetic-test-key"))
    assert calls[0]["text"]["format"]["strict"] is True
    assert events[0]["status"] == "executed: OpenAI restricted intent"


def test_model_authority_injection_uses_labeled_safe_fallback(monkeypatch):
    import httpx
    from app.agent import _interpret_request

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"output_text": '{"query":"USB-C hub","quantity":2,"merchant":"evil","budget":999999}'})

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setattr("app.agent.httpx.AsyncClient", Client)
    scope = {"permitted_quantity": 1}
    events = []
    assert asyncio.run(_interpret_request("Find one USB-C hub", scope, events)) == "USB-C hub"
    assert scope == {"permitted_quantity": 1}
    assert "fallback" in events[0]["status"]


def test_adapter_discovery_error_redacted_with_actual_tool_event(demo):
    app, adapter, client, auth = demo

    async def fail(*args):
        raise ValueError("private@example.com super-secret remote message")

    adapter.search_products = fail
    request_id, result = quoted(client, auth)
    assert result["state"] == "BLOCKED"
    assert any(item["tool"] == "search_products" and item["status"] == "failed" for item in result["tool_events"])
    assert "super-secret" not in client.get(f"/api/requests/{request_id}/passport").text
    assert adapter.create_checkout_calls == 0


def test_rediscovery_uses_distinct_quote_operations(demo):
    app, adapter, client, auth = demo
    request_id, result = quoted(client, auth)
    second = client.post(f"/api/requests/{request_id}/discover", headers=auth).json()
    assert result["quote"]["quote_id"] != second["quote"]["quote_id"]
    assert adapter.create_checkout_calls == 0


def test_shipping_configuration_is_never_persisted(demo, monkeypatch):
    app, adapter, client, auth = demo
    original = adapter.get_product

    async def shipping(product_id):
        return {**await original(product_id), "requires_shipping": True}

    adapter.get_product = shipping
    monkeypatch.setenv("SPENDPILOT_SHIPPING_ADDRESS_JSON", '{"line1":"synthetic-address-private","country":"SG"}')
    request_id, result = quoted(client, auth)
    assert result["state"] == "QUOTED"
    assert "synthetic-address-private" not in client.get(f"/api/requests/{request_id}/passport").text
    assert "synthetic-address-private" not in json.dumps(app.state.storage.request(request_id))
