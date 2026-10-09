from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agent import DiscoveryError, FakeReapAdapter, discover, safe_candidate, safe_quote
from app.models import CheckoutInput, PolicyTestOutcomeInput, RequestInput
from app.passport import project_passport
from app.policy import evaluate_purchase
from app.purchases import CheckoutService
from app.storage import Storage

ROOT = Path(__file__).resolve().parent.parent


def _cap(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be positive integer SGD cents")
    return value


def _scope(mode: str) -> dict[str, Any]:
    fixture = mode == "policy_test"
    return {"scope_id": f"harbour-studio-{mode}-v1", "business_label": "Harbour Studio",
            "trusted_owner_id": "trusted-owner" if fixture else os.getenv("REAP_TRUSTED_OWNER_ID", ""),
            "enrollment_id": "fixture-enrollment" if fixture else os.getenv("REAP_ENROLLMENT_ID", ""),
            "currency": "SGD", "market": "SG", "merchant_key": "fixture-approved-merchant" if fixture else os.getenv("REAP_MERCHANT_KEY", ""),
            "approved_product_type": "USB-C Hub", "permitted_quantity": 1,
            "per_checkout_cap_minor": _cap("SPENDPILOT_CHECKOUT_CAP_MINOR", 10_000),
            "total_budget_minor": _cap("SPENDPILOT_RUN_CAP_MINOR", 20_000), "confirmed_at": "", "version": 1}


def _live_adapter() -> Any | None:
    required = ("REAP_API_KEY", "REAP_BASE_URL", "REAP_VERSION", "REAP_ENROLLMENT_ID", "REAP_MERCHANT_KEY", "REAP_TRUSTED_OWNER_ID")
    if not all(os.getenv(key) for key in required):
        return None
    host = urlsplit(os.environ["REAP_BASE_URL"])
    if host.scheme != "https" or host.hostname not in {"sandbox.api.reap.global", "sg.sandbox.api.reap.global"}:
        raise RuntimeError("confirm an official Reap sandbox host before starting live mode")
    try:
        from app.reap_adapter import ReapAdapter
    except ImportError:
        return None
    return ReapAdapter()


def _is_active_trusted_enrollment(scope: dict[str, Any], enrollment: Any) -> bool:
    return (
        isinstance(enrollment, dict)
        and enrollment.get("status") == "ACTIVE"
        and enrollment.get("owner_id") == scope["trusted_owner_id"]
        and enrollment.get("enrollment_id") == scope["enrollment_id"]
    )


def _local_payment_return_url(value: str, origin: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return value == f"{origin}/payment/return" and parts.path == "/payment/return"


def _public_payment_return_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not parts.username
        and not parts.password
        and parts.path == "/payment/return"
        and not parts.query
        and not parts.fragment
        and urlsplit(value)._replace(query="", fragment="").geturl() == value
    )


def _public_attempt(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    return {key: attempt.get(key) for key in ("attempt_id", "checkout_id", "checkout_status", "settlement_state", "order_id", "final_amount_minor", "reserved_minor", "created_at", "updated_at")}


def _public_request(record: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": record["request_id"], "state": record["state"], "candidate": record["candidate"], "quote": record["quote"],
            "attempt": _public_attempt(record["attempt"]), "source_mode": record.get("source_mode")}


def create_app(*, storage_path: str | None = None, adapter: Any | None = None, mode: str | None = None) -> FastAPI:
    selected_mode = mode or os.getenv("SPENDPILOT_MODE", "live")
    if selected_mode not in {"live", "policy_test"}:
        raise RuntimeError("SPENDPILOT_MODE must be live or policy_test")
    reap = adapter if adapter is not None else (FakeReapAdapter() if selected_mode == "policy_test" else _live_adapter())
    if selected_mode == "live" and isinstance(reap, FakeReapAdapter):
        raise RuntimeError("fixture adapters cannot run in live mode")
    storage = Storage(storage_path or os.getenv("SPENDPILOT_DB_PATH", f"spendpilot-{selected_mode}.db"))
    storage.seed_scope(_scope(selected_mode))
    app = FastAPI(title="SpendPilot", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.state.storage, app.state.adapter, app.state.mode = storage, reap, selected_mode
    app.state.sessions: dict[str, str] = {}
    origin = os.getenv("SPENDPILOT_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise RuntimeError("this single-operator demo requires a loopback HTTP origin")
    app.state.origin = origin
    local_return_url = origin + "/payment/return"
    return_url = os.getenv("REAP_RETURN_URL", local_return_url) if selected_mode == "live" else local_return_url
    if selected_mode == "live" and reap is not None and not _public_payment_return_url(return_url):
        raise RuntimeError("live Reap callbacks require a clean HTTPS /payment/return URL")
    if selected_mode == "live" and reap is None and not (_public_payment_return_url(return_url) or _local_payment_return_url(return_url, origin)):
        raise RuntimeError("REAP_RETURN_URL must be a clean HTTPS or configured local /payment/return route")
    app.state.checkout = CheckoutService(storage, reap, selected_mode, return_url)

    @app.middleware("http")
    async def local_demo(request: Request, call_next: Any) -> Any:
        try:
            local_peer = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local_peer = False
        if not local_peer or request.headers.get("host") != parsed.netloc:
            return JSONResponse({"error": {"code": "LOCAL_ONLY", "message": "this demo accepts its configured loopback origin only"}}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        codes = {403: "AUTHORIZATION_REQUIRED", 404: "NOT_FOUND", 409: "STATE_CONFLICT", 503: "REAP_NOT_READY"}
        return JSONResponse({"error": {"code": codes.get(error.status_code, "REQUEST_FAILED"), "message": str(error.detail)}, "detail": str(error.detail)}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def input_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "request fields do not match the strict application contract"}}, status_code=422)

    def guard(request: Request) -> None:
        expected = app.state.sessions.get(request.cookies.get("spendpilot_session", ""))
        csrf = request.headers.get("x-csrf-token")
        if request.headers.get("origin") != origin or not expected or not csrf or not secrets.compare_digest(expected, csrf):
            raise HTTPException(403, "same-origin session authorization and explicit interaction are required")

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> FileResponse:
        response = FileResponse(ROOT / "templates" / "index.html")
        session = request.cookies.get("spendpilot_session")
        if session not in app.state.sessions:
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            app.state.sessions[session] = csrf
            response.set_cookie("spendpilot_session", session, httponly=True, samesite="lax")
            response.set_cookie("spendpilot_csrf", csrf, samesite="lax")
        return response

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "mode": selected_mode, "reap_ready": reap is not None}

    @app.get("/api/scope")
    async def scope() -> dict[str, Any]:
        config = storage.scope()
        try:
            enrollment = await reap.get_enrollment(config["enrollment_id"]) if reap is not None else None
        except Exception:
            enrollment = None
        return {**{key: config[key] for key in ("business_label", "currency", "market", "merchant_key", "approved_product_type", "permitted_quantity", "per_checkout_cap_minor")},
                "budget": storage.budget_snapshot(), "enrollment_ready": _is_active_trusted_enrollment(config, enrollment), "mode": selected_mode,
                "agent_mode": "OPENAI AVAILABLE" if os.getenv("OPENAI_API_KEY") else "RULE-BASED DEMO INPUT",
                "scope_confirmed": bool(config["confirmed_at"]), "suspended": bool(config["suspended"])}

    @app.post("/api/requests")
    async def create_request(payload: RequestInput, request: Request) -> dict[str, Any]:
        guard(request)
        record = storage.create_request(payload.text)
        return {"request_id": record["request_id"], "scope_summary": {"business": storage.scope()["business_label"], "version": record["scope_version"]}, "state": "DRAFT"}

    @app.post("/api/requests/{request_id}/discover")
    async def run_discovery(request_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            record = storage.request(request_id)
        except KeyError as error:
            raise HTTPException(404, "request not found") from error
        if reap is None:
            raise HTTPException(503, "live Reap adapter and trusted sandbox configuration are not ready")
        if record["attempt"]:
            raise HTTPException(409, "a checkout attempt already exists for this request")
        source = "REAP_RESPONSE" if selected_mode == "live" else "POLICY_TEST"
        try:
            config = storage.scope()
            tools = [{"tool": "get_enrollment", "status": "started"}]
            try:
                enrollment = await reap.get_enrollment(config["enrollment_id"])
                tools[-1]["status"] = "executed"
            except Exception:
                tools[-1]["status"] = "failed"
                enrollment = None
            if not _is_active_trusted_enrollment(config, enrollment):
                raise DiscoveryError("an ACTIVE Reap enrollment for the configured trusted owner is required before catalog search; no checkout called", tools)
            candidate, quote, discovery_tools = await discover(reap, config, record["user_text"], request_id)
            tools.extend(discovery_tools)
            storage.save_discovery(request_id, candidate, quote, selected_mode)
            updated = storage.request(request_id)
            decision = evaluate_purchase(config, candidate, quote, storage.budget_snapshot(), enrollment, mode=selected_mode, request=updated)
            storage.add_event(request_id, source, "discovery", {"candidate": safe_candidate(candidate), "quote": safe_quote(quote), "tools": tools})
            storage.add_event(request_id, "APP_POLICY", "discovery_evaluated", {"allowed": decision.allowed, "rules": decision.rules})
            if not decision.allowed:
                storage.set_state(request_id, "BLOCKED")
            return {"state": "QUOTED" if decision.allowed else "BLOCKED", "candidate": candidate, "quote": quote,
                    "rule_results": decision.rules, "tool_events": tools, "checkout_created": False,
                    "fixture_notice": "POLICY TEST - NOT LIVE REAP" if selected_mode == "policy_test" else None}
        except Exception as error:
            # Adapter error messages/bodies can contain personal data or secrets.
            detail = str(error) if isinstance(error, ValueError) and error.__class__.__module__ == "app.agent" else "discovery could not verify an eligible quote; no checkout called"
            tools = getattr(error, "events", [])
            storage.mark_blocked(request_id, detail)
            storage.add_event(request_id, source if selected_mode == "policy_test" else "APP_POLICY", "discovery_stopped", {"tools": tools, "detail": detail})
            rule = "active_trusted_enrollment" if detail.startswith("an ACTIVE Reap enrollment") else "verified_discovery"
            return {"state": "BLOCKED", "candidate": None, "quote": None,
                    "rule_results": [{"rule": rule, "outcome": "FAIL", "detail": detail, "source": "APP_POLICY"}],
                    "tool_events": tools, "checkout_created": False, "fixture_notice": "POLICY TEST - NOT LIVE REAP" if selected_mode == "policy_test" else None}

    @app.post("/api/requests/{request_id}/checkout")
    async def checkout(request_id: str, payload: CheckoutInput, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            result = await app.state.checkout.guarded_create_checkout(request_id, payload.confirm)
        except KeyError as error:
            raise HTTPException(404, "request not found") from error
        if result["blocked"]:
            return {"state": result.get("state", "BLOCKED"), "reasons": result["reasons"], "checkout_created": False, "rule_results": result["rules"]}
        attempt = result["attempt"]
        return {"state": attempt["checkout_status"], "checkout_id": attempt["checkout_id"], "checkout_created": result["created"],
                "next_action_url": result.get("next_action_url"), "reasons": result["reasons"], "source_mode": selected_mode, "attempt": _public_attempt(attempt)}

    def public_status(request_id: str) -> dict[str, Any]:
        value = _public_request(storage.request(request_id))
        evaluations = [event for event in storage.events(request_id) if event["event_type"] == "discovery_evaluated"]
        value["rule_results"] = evaluations[-1]["payload"]["rules"] if evaluations else []
        return value

    @app.post("/api/requests/{request_id}/policy-test-outcome")
    async def policy_test_outcome(request_id: str, payload: PolicyTestOutcomeInput, request: Request) -> dict[str, Any]:
        guard(request)
        if selected_mode != "policy_test" or not isinstance(reap, FakeReapAdapter):
            raise HTTPException(404, "policy-test outcomes are unavailable")
        try:
            record = storage.request(request_id)
        except KeyError as error:
            raise HTTPException(404, "request not found") from error
        attempt = record["attempt"]
        if not attempt or attempt["settlement_state"] != "HELD" or attempt["checkout_status"] not in {"REQUIRES_ACTION", "PROCESSING"}:
            raise HTTPException(409, "a pending fixture checkout is required")
        try:
            reap.set_checkout_outcome(attempt["checkout_id"], payload.outcome)
        except ValueError as error:
            raise HTTPException(409, "fixture checkout cannot be updated") from error
        # Reconciliation owns all ledger and terminal-state handling.
        await app.state.checkout.reconcile(request_id)
        value = public_status(request_id)
        value["fixture_notice"] = "POLICY TEST - SIMULATED OUTCOME; NO REAL PAYMENT"
        return value

    @app.get("/api/requests/{request_id}")
    async def request_status(request_id: str) -> dict[str, Any]:
        try:
            return public_status(request_id)
        except KeyError as error:
            raise HTTPException(404, "request not found") from error

    @app.get("/api/requests/{request_id}/passport")
    async def passport(request_id: str) -> dict[str, Any]:
        try:
            record = storage.request(request_id)
        except KeyError as error:
            raise HTTPException(404, "request not found") from error
        return project_passport(record, storage.events(request_id), selected_mode)

    @app.post("/api/requests/{request_id}/refresh")
    async def refresh(request_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            return _public_request(await app.state.checkout.reconcile(request_id))
        except KeyError as error:
            raise HTTPException(404, "request not found") from error

    @app.get("/payment/return", include_in_schema=False)
    async def payment_return(state: str = "") -> RedirectResponse:
        attempt = storage.attempt_for_state(state)
        if attempt is None:
            raise HTTPException(404, "unknown payment return")
        await app.state.checkout.reconcile(attempt["request_id"])
        return RedirectResponse(url=f"/?request_id={attempt['request_id']}", status_code=303)

    return app


app = create_app()
