from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import PolicyDecision


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def evaluate_purchase(scope: dict[str, Any], candidate: dict[str, Any] | None, quote: dict[str, Any] | None,
                      budget_snapshot: dict[str, int], enrollment: dict[str, Any] | None, *, mode: str,
                      request: dict[str, Any] | None = None) -> PolicyDecision:
    """All spending authority comes from persisted scope and the selected quote binding."""
    candidate, quote, enrollment, request = candidate or {}, quote or {}, enrollment or {}, request or {}
    total, available = quote.get("total_minor"), budget_snapshot.get("available_minor")
    valid_total = type(total) is int and total > 0
    cap = scope.get("per_checkout_cap_minor")
    valid_cap = type(cap) is int and cap > 0
    source = "POLICY_TEST" if mode == "policy_test" else "REAP_RESPONSE"
    current = datetime.now(UTC)
    confirmed, expiry = _timestamp(scope.get("confirmed_at")), _timestamp(quote.get("expires_at"))
    rules: list[dict[str, str]] = []

    def rule(name: str, passed: bool, detail: str, origin: str = "APP_POLICY", known: bool = True) -> None:
        rules.append({"rule": name, "outcome": "UNKNOWN" if not known else "PASS" if passed else "FAIL", "detail": detail, "source": origin})

    rule("scope_confirmed", bool(confirmed and current - timedelta(hours=12) <= confirmed <= current) and not scope.get("suspended")
         and request.get("scope_version") == scope.get("version") and request.get("scope_id") == scope.get("scope_id"),
         "scope was explicitly confirmed, is current, and is not suspended", known=confirmed is not None)
    rule("active_trusted_enrollment", bool(scope.get("trusted_owner_id")) and bool(scope.get("enrollment_id"))
         and enrollment.get("status") == "ACTIVE" and enrollment.get("owner_id") == scope.get("trusted_owner_id")
         and enrollment.get("enrollment_id") == scope.get("enrollment_id"), "ACTIVE enrollment matches the trusted owner and ID", source,
         known=all(enrollment.get(key) is not None for key in ("status", "owner_id", "enrollment_id")))
    rule("selected_product_binding", bool(candidate.get("product_id")) and bool(candidate.get("variant_id"))
         and candidate.get("product_id") == request.get("product_id") and candidate.get("variant_id") == request.get("variant_id")
         and candidate.get("merchant_key") == request.get("merchant_key") and type(request.get("quantity")) is int
         and request.get("quantity") == scope.get("permitted_quantity") == 1, "stored product, variant, merchant and quantity match",
         known=bool(candidate.get("product_id") and candidate.get("variant_id")))
    rule("approved_merchant", bool(scope.get("merchant_key")) and candidate.get("merchant_key") == scope.get("merchant_key"),
         "exact configured canonical merchant matches", source, known=bool(scope.get("merchant_key") and candidate.get("merchant_key")))
    rule("availability", candidate.get("available") is True, "availability is explicitly true; hardware specifications remain unverified", source,
         known=type(candidate.get("available")) is bool)
    rule("market_and_currency", scope.get("market") == "SG" and scope.get("currency") == quote.get("currency") == "SGD",
         "trusted market is SG and quoted currency is SGD", source, known=bool(quote.get("currency")))
    rule("valid_quote", bool(quote.get("quote_id")) and quote.get("quote_id") == request.get("quote_id") and valid_total
         and bool(expiry and expiry > current), "stored quote ID, positive exact integer cents and expiry are verified", source,
         known=all(quote.get(key) is not None for key in ("quote_id", "total_minor", "expires_at")))
    rule("quote_source", mode in {"live", "policy_test"} and request.get("source_mode") == mode, "quote evidence matches the explicit execution mode", source)
    rule("per_checkout_cap", valid_total and valid_cap and total <= cap, "final quoted amount fits the per-checkout cap")
    rule("run_budget", valid_total and type(available) is int and total <= available, "completed debits and held reservations are deducted from run budget")
    rule("no_pending_attempt", not request.get("has_pending_attempt") and not request.get("attempt"), "no prior attempt exists for this request version")
    return PolicyDecision(allowed=all(item["outcome"] == "PASS" for item in rules), rules=rules)
