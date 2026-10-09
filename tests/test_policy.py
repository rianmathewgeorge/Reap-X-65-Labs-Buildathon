from datetime import UTC, datetime, timedelta

import pytest

from app.policy import evaluate_purchase


def inputs(total=7290):
    scope = {"scope_id": "scope", "confirmed_at": datetime.now(UTC).isoformat(), "suspended": 0, "version": 1,
             "trusted_owner_id": "owner", "enrollment_id": "enr", "merchant_key": "merchant", "market": "SG", "currency": "SGD",
             "permitted_quantity": 1, "per_checkout_cap_minor": 10_000}
    candidate = {"product_id": "product", "variant_id": "variant", "merchant_key": "merchant", "available": True}
    quote = {"quote_id": "quote", "total_minor": total, "currency": "SGD", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    request = {"scope_id": "scope", "scope_version": 1, "product_id": "product", "variant_id": "variant", "merchant_key": "merchant",
               "quantity": 1, "quote_id": "quote", "source_mode": "live", "has_pending_attempt": False}
    enrollment = {"enrollment_id": "enr", "status": "ACTIVE", "owner_id": "owner"}
    return scope, candidate, quote, request, enrollment


def decision(parts, available=20_000, mode="live"):
    scope, candidate, quote, request, enrollment = parts
    return evaluate_purchase(scope, candidate, quote, {"available_minor": available}, enrollment, mode=mode, request=request)


@pytest.mark.parametrize("total,allowed", [(7290, True), (10000, True), (10001, False), (None, False), (True, False), (7290.0, False), (72.9, False), (0, False), (-1, False), ("7290", False)])
def test_exact_minor_amount_and_cap(total, allowed):
    assert decision(inputs(total)).allowed is allowed


@pytest.mark.parametrize("index,key,value", [
    (0, "confirmed_at", ""), (0, "suspended", 1), (0, "version", 2), (0, "market", "US"), (0, "merchant_key", None),
    (1, "merchant_key", "unapproved"), (1, "variant_id", None), (1, "product_id", "different"), (1, "available", None), (1, "available", False),
    (2, "currency", "USD"), (2, "quote_id", "different"), (2, "expires_at", "2020-01-01T00:00:00Z"),
    (2, "expires_at", "2099-01-01T00:00:00"), (2, "expires_at", None),
    (3, "quantity", 2), (3, "quantity", True), (3, "scope_version", 2), (3, "has_pending_attempt", True), (3, "source_mode", "policy_test"),
    (4, "owner_id", None), (4, "owner_id", "other"), (4, "enrollment_id", "other"), (4, "status", "PENDING"),
])
def test_mandatory_facts_fail_closed(index, key, value):
    parts = inputs()
    parts[index][key] = value
    assert not decision(parts).allowed


def test_budget_and_fixture_source():
    parts = inputs()
    assert not decision(parts, available=7289).allowed
    parts[3]["source_mode"] = "policy_test"
    fixture = decision(parts, mode="policy_test")
    assert fixture.allowed
    assert any(item["source"] == "POLICY_TEST" for item in fixture.rules)
    assert not decision(parts, mode="live").allowed
