from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

# The buildathon repo is intentionally not packaged yet; make its root importable
# when pytest is launched through either the local venv or the user's shell Python.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.reap_adapter import ReapAdapter
from app.reap_client import (
    ReapAPIError,
    ReapCheckoutOutcomeUnknown,
    ReapClient,
    ReapMalformedResponse,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def adapter_for(handler):
    client = ReapClient(
        base_url="https://sandbox.example.test",
        api_key="test-key-not-a-real-secret",
        version="2025-02-14",
        transport=httpx.MockTransport(handler),
    )
    return ReapAdapter(client)


@pytest.mark.asyncio
async def test_search_normalizes_preview_variant_and_required_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=fixture("reap_search.json"))

    adapter = adapter_for(handler)
    try:
        result = await adapter.search_products("USB-C hub")
    finally:
        await adapter.client.aclose()

    assert result == [
        {
            "product_id": "prd_sanitized_hub",
            "name": "Sanitized USB-C Hub",
            "merchant_name": "UGREEN SG",
            "merchant_key": "UGREEN SG",
            "variant_id": "var_sanitized_hub",
            "available": True,
            "requires_shipping": None,
            "raw_evidence": {
                "product_id": "prd_sanitized_hub",
                "merchant_name": "UGREEN SG",
                "variant_id": "var_sanitized_hub",
                "available": True,
                "requires_shipping": None,
            },
        }
    ]
    request = seen["request"]
    assert request.headers["authorization"] == "Bearer test-key-not-a-real-secret"
    assert request.headers["reap-version"] == "2025-02-14"
    assert "idempotency-key" not in request.headers
    assert json.loads(request.content) == {
        "query": "USB-C hub",
        "context": {"country": "SG", "currency": "SGD"},
        "filters": {"availability": "AVAILABLE_ONLY"},
    }


@pytest.mark.asyncio
async def test_product_details_uses_default_variant():
    adapter = adapter_for(lambda _: httpx.Response(200, json=fixture("reap_detail.json")))
    try:
        product = await adapter.get_product("prd_sanitized_hub")
    finally:
        await adapter.client.aclose()
    assert product["variant_id"] == "var_sanitized_hub"
    assert product["requires_shipping"] is None


@pytest.mark.asyncio
async def test_quote_uses_final_amount_and_preserves_expiry():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=fixture("reap_quote.json"))

    adapter = adapter_for(handler)
    try:
        quote = await adapter.create_quote(
            "var_sanitized_hub",
            1,
            "owner@example.test",
            {"firstName": "Test", "lastName": "Owner"},
            "quote-operation-key",
        )
    finally:
        await adapter.client.aclose()
    assert quote["total_minor"] == 5999
    assert quote["subtotal_minor"] == 5999
    assert quote["shipping_minor"] == 0
    assert quote["tax_minor"] == 0
    assert quote["selected_shipping"] == "Free shipping"
    assert quote["expires_at"] == "2030-01-01T00:00:00Z"
    assert seen["request"].headers["idempotency-key"] == "quote-operation-key"
    assert "owner@example.test" not in str(quote["raw_evidence"])


@pytest.mark.asyncio
async def test_quote_rejects_fractional_cents_and_wrong_currency():
    bad = fixture("reap_quote.json")
    bad["amountBreakdown"]["finalAmount"] = {"amount": "59.991", "currency": "SGD"}
    adapter = adapter_for(lambda _: httpx.Response(200, json=bad))
    try:
        with pytest.raises(ReapMalformedResponse, match="fractions smaller"):
            await adapter.get_quote("quote_sanitized")
    finally:
        await adapter.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["REQUIRES_ACTION", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED", "UNKNOWN_NEW"])
async def test_checkout_statuses_are_preserved_exactly(status):
    body = fixture("reap_checkout_requires_action.json")
    body["status"] = status
    adapter = adapter_for(lambda _: httpx.Response(200, json=body))
    try:
        checkout = await adapter.get_checkout("checkout_sanitized")
    finally:
        await adapter.client.aclose()
    assert checkout["status"] == status
    assert checkout["checkout_id"] == "checkout_sanitized"
    assert checkout["next_action_url"] == "https://hosted.example.test/approval/redacted"


@pytest.mark.asyncio
async def test_checkout_timeout_is_outcome_unknown_not_success_or_failure():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    adapter = adapter_for(handler)
    try:
        with pytest.raises(ReapCheckoutOutcomeUnknown):
            await adapter.create_checkout(
                "quote_sanitized", "enrollment_sanitized", "https://app.example.test/payment/return", "checkout-key"
            )
    finally:
        await adapter.client.aclose()


@pytest.mark.asyncio
async def test_checkout_connection_error_is_also_outcome_unknown():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    adapter = adapter_for(handler)
    try:
        with pytest.raises(ReapCheckoutOutcomeUnknown):
            await adapter.create_checkout(
                "quote_sanitized", "enrollment_sanitized", "https://app.example.test/payment/return", "checkout-key"
            )
    finally:
        await adapter.client.aclose()


@pytest.mark.asyncio
async def test_api_error_has_safe_code_and_status():
    adapter = adapter_for(
        lambda _: httpx.Response(422, json={"error": {"code": "VALIDATION_FAILED", "message": "Validation failed", "detail": {"on": "body"}}})
    )
    try:
        with pytest.raises(ReapAPIError) as raised:
            await adapter.get_enrollment("enrollment_sanitized")
    finally:
        await adapter.client.aclose()
    assert raised.value.code == "VALIDATION_FAILED"
    assert raised.value.status == 422
    assert "VALIDATION_FAILED" in str(raised.value)


@pytest.mark.asyncio
async def test_enrollment_creation_has_documented_external_redirect_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json={
                "id": "enrollment_sanitized",
                "status": "REQUIRES_ACTION",
                "owner": {"id": "trusted-owner"},
                "nextAction": {"url": "https://hosted.example.test/enroll/redacted"},
            },
        )

    adapter = adapter_for(handler)
    try:
        enrollment = await adapter.create_external_enrollment(
            "trusted-owner", "owner@example.test", "https://app.example.test/payment/return", "enrollment-key"
        )
    finally:
        await adapter.client.aclose()
    assert enrollment["status"] == "REQUIRES_ACTION"
    assert seen["request"].headers["idempotency-key"] == "enrollment-key"
    assert json.loads(seen["request"].content) == {
        "source": "EXTERNAL",
        "owner": {"type": "CLIENT_REFERENCE", "id": "trusted-owner", "email": "owner@example.test"},
        "presentation": {"type": "REDIRECT", "returnUrl": "https://app.example.test/payment/return"},
    }
