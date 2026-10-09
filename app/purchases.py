from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.policy import evaluate_purchase


class CheckoutService:
    def __init__(self, storage: Any, adapter: Any, mode: str, return_url: str) -> None:
        self.storage, self.adapter, self.mode, self.return_url = storage, adapter, mode, return_url

    async def guarded_create_checkout(self, request_id: str, operator_confirmed: bool = True) -> dict[str, Any]:
        request, scope = self.storage.request(request_id), self.storage.scope()
        if operator_confirmed is not True:
            return self._blocked(request_id, [self._failure("operator_confirmation", "explicit local confirmation is required")]) if not request["attempt"] else self._existing(request["attempt"])
        if request["attempt"]:
            return self._existing(request["attempt"])
        if self.adapter is None or not request["quote_id"] or request["state"] != "QUOTED":
            return self._blocked(request_id, [self._failure("reap_readiness", "a reviewed quote and configured adapter are required")])
        source = "POLICY_TEST" if self.mode == "policy_test" else "REAP_RESPONSE"
        try:
            enrollment = await self.adapter.get_enrollment(scope["enrollment_id"])
            refreshed = await self.adapter.get_quote(request["quote_id"])
            if not isinstance(enrollment, dict) or not isinstance(refreshed, dict):
                raise ValueError("invalid normalized response")
        except Exception:
            return self._blocked(request_id, [self._failure("reap_refresh", "could not verify current enrollment and quote; no checkout called")])
        self.storage.add_event(request_id, source, "quote_refreshed", {key: refreshed.get(key) if type(refreshed.get(key)) in (str, int) else None for key in ("quote_id", "total_minor", "currency", "expires_at")})
        if any(refreshed.get(key) != (request["quote"] or {}).get(key) for key in ("quote_id", "total_minor", "currency", "expires_at")):
            self.storage.mark_review_required(request_id, "refreshed quote changed; discover and review again")
            return {"attempt": None, "created": False, "blocked": True, "state": "REVIEW_REQUIRED", "reasons": ["quote_changed"], "rules": []}
        decision = evaluate_purchase(scope, request["candidate"], refreshed, self.storage.budget_snapshot(), enrollment, mode=self.mode, request=request)
        self.storage.add_event(request_id, "APP_POLICY", "checkout_evaluated", {"allowed": decision.allowed, "rules": decision.rules})
        if not decision.allowed:
            return self._blocked(request_id, decision.rules)
        return_state = secrets.token_urlsafe(32)
        parts = urlsplit(self.return_url)
        query = dict(parse_qsl(parts.query))
        query["state"] = return_state
        return_url = urlunsplit(parts._replace(query=urlencode(query)))
        payload = {"quote_id": request["quote_id"], "enrollment_id": scope["enrollment_id"], "return_url": return_url}
        try:
            attempt, created = self.storage.reserve_attempt(request_id, request, refreshed["total_minor"], payload, return_state)
        except ValueError as error:
            return self._blocked(request_id, [self._failure("atomic_reservation", str(error))])
        if not created:
            return self._existing(attempt)
        try:
            # Use exactly the operation recorded before the network call.
            body = json.loads(attempt["request_body_json"])
            checkout = await self.adapter.create_checkout(body["quote_id"], body["enrollment_id"], body["return_url"], attempt["idempotency_key"])
            if not isinstance(checkout, dict):
                raise ValueError("malformed normalized checkout")
            checkout = {key: checkout.get(key) for key in ("checkout_id", "status", "next_action_url", "order_id", "final_amount_minor", "currency")}
            checkout["next_action_url"] = self._hosted_url(checkout["next_action_url"])
            attempt = self.storage.update_attempt(request_id, checkout)
            self.storage.add_event(request_id, source, "checkout_response", {key: checkout.get(key) for key in ("checkout_id", "status", "order_id", "final_amount_minor", "currency")})
            reasons = []
            if self.mode == "live" and not attempt["next_action_url"]:
                reasons = ["No hosted approval URL returned; organiser must clarify approval behavior before further purchases."]
                self.storage.connection.execute("UPDATE scopes SET suspended=1")
                self.storage.add_event(request_id, "APP_POLICY", "hosted_approval_unverified", {"detail": reasons[0]})
            return {"attempt": attempt, "created": bool(attempt["checkout_id"]), "blocked": False, "reasons": reasons, "next_action_url": attempt["next_action_url"]}
        except Exception:
            attempt = self.storage.update_attempt(request_id, {}, unknown=True)
            self.storage.add_event(request_id, "APP_POLICY", "checkout_outcome_unknown", {"detail": "POST attempted; reservation retained; no automatic retry"})
            return {"attempt": attempt, "created": None, "blocked": False, "reasons": ["Checkout outcome unknown; reservation retained. Do not retry with a new key."]}

    @staticmethod
    def _failure(rule: str, detail: str) -> dict[str, str]:
        return {"rule": rule, "outcome": "FAIL", "detail": detail, "source": "APP_POLICY"}

    def _blocked(self, request_id: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        # A concurrent click may have reserved while this request awaited Reap.
        existing = self.storage.request(request_id)["attempt"]
        if existing:
            return self._existing(existing)
        self.storage.add_event(request_id, "APP_POLICY", "checkout_blocked", {"rules": rules, "checkout_post_called": False})
        self.storage.set_state(request_id, "BLOCKED")
        return {"attempt": None, "created": False, "blocked": True, "state": "BLOCKED", "reasons": [rule["rule"] for rule in rules if rule["outcome"] != "PASS"], "rules": rules}

    def _existing(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return {"attempt": attempt, "created": True if attempt["checkout_id"] else None, "blocked": False, "reasons": [],
                "next_action_url": self._hosted_url(attempt["next_action_url"]) if attempt["checkout_status"] == "REQUIRES_ACTION" else None}

    @staticmethod
    def _hosted_url(value: Any) -> str | None:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            return None
        try:
            parts = urlsplit(value)
            return value if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password else None
        except ValueError:
            return None

    async def reconcile(self, request_id: str) -> dict[str, Any]:
        request = self.storage.request(request_id)
        attempt = request["attempt"]
        if not attempt or not attempt["checkout_id"] or self.adapter is None or attempt["settlement_state"] != "HELD":
            return request
        try:
            checkout = await self.adapter.get_checkout(attempt["checkout_id"])
            if not isinstance(checkout, dict):
                raise ValueError("malformed normalized checkout")
            checkout = {key: checkout.get(key) for key in ("checkout_id", "status", "next_action_url", "order_id", "final_amount_minor", "currency")}
            checkout["next_action_url"] = self._hosted_url(checkout["next_action_url"])
            self.storage.update_attempt(request_id, checkout)
            self.storage.add_event(request_id, "POLICY_TEST" if self.mode == "policy_test" else "REAP_RESPONSE", "checkout_status_refreshed",
                                   {key: checkout.get(key) for key in ("checkout_id", "status", "order_id", "final_amount_minor", "currency")})
        except Exception:
            self.storage.add_event(request_id, "APP_POLICY", "status_lookup_unavailable", {"detail": "previous status and reservation retained"})
        return self.storage.request(request_id)
