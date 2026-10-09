"""Stable normalized Reap API interface used by SpendPilot's policy layer."""

from __future__ import annotations

import math
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from .reap_client import ReapClient, ReapMalformedResponse


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReapMalformedResponse(f"Reap response is missing {field}")
    return value


def _as_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_amount_minor(value: Any, *, currency: str | None) -> int | None:
    """Convert Reap's documented major-unit SGD amount without using floats."""
    if value is None:
        return None
    amount_object = _as_object(value)
    # Some sandbox tax responses nest the money object one level further.
    if isinstance(amount_object.get("amount"), dict):
        amount_object = amount_object["amount"]
    amount = amount_object.get("amount") if amount_object else value
    value_currency = amount_object.get("currency") if amount_object else currency
    if value_currency and currency and value_currency != currency:
        raise ReapMalformedResponse("Reap amount currency does not match quote currency")
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ReapMalformedResponse("Reap returned an invalid monetary amount") from exc
    if not decimal_amount.is_finite():
        raise ReapMalformedResponse("Reap returned a non-finite monetary amount")
    minor = decimal_amount * Decimal("100")
    if minor != minor.to_integral_value():
        raise ReapMalformedResponse("Reap amount has fractions smaller than one cent")
    return int(minor)


def _required_total_minor(money: Any) -> tuple[int, str]:
    object_value = _as_object(money)
    currency = _nonempty_string(object_value.get("currency"), "final amount currency")
    if currency != "SGD":
        raise ReapMalformedResponse(f"Unsupported quote currency: {currency}")
    minor = _optional_amount_minor(object_value, currency=currency)
    if minor is None or minor <= 0:
        raise ReapMalformedResponse("Reap final amount must be positive")
    return minor, currency


def _next_action_url(body: dict[str, Any]) -> str | None:
    next_action = _as_object(body.get("nextAction"))
    url = next_action.get("url")
    return url if isinstance(url, str) and url else None


class ReapAdapter:
    """Normalized, deliberately narrow facade over documented Agentic endpoints."""

    def __init__(self, client: ReapClient | None = None) -> None:
        self.client = client or ReapClient()

    async def get_enrollment(self, enrollment_id: str) -> dict:
        body = await self.client.request("GET", f"/agentic/enrollments/{enrollment_id}")
        return {
            "enrollment_id": _nonempty_string(body.get("id"), "enrollment id"),
            "status": _nonempty_string(body.get("status"), "enrollment status"),
            "owner_id": _as_object(body.get("owner")).get("id"),
            "next_action_url": _next_action_url(body),
        }

    async def create_external_enrollment(
        self, owner_id: str, owner_email: str, return_url: str, idempotency_key: str
    ) -> dict:
        body = await self.client.request(
            "POST",
            "/agentic/enrollments",
            idempotency_key=idempotency_key,
            json_body={
                "source": "EXTERNAL",
                "owner": {"type": "CLIENT_REFERENCE", "id": owner_id, "email": owner_email},
                "presentation": {"type": "REDIRECT", "returnUrl": return_url},
            },
        )
        return {
            "enrollment_id": _nonempty_string(body.get("id"), "enrollment id"),
            "status": _nonempty_string(body.get("status"), "enrollment status"),
            "owner_id": _as_object(body.get("owner")).get("id") or owner_id,
            "next_action_url": _next_action_url(body),
        }

    async def search_products(self, query: str, country: str = "SG", currency: str = "SGD") -> list[dict]:
        body = await self.client.request(
            "POST",
            "/agentic/products/search",
            json_body={
                "query": query,
                "context": {"country": country, "currency": currency},
                "filters": {"availability": "AVAILABLE_ONLY"},
            },
        )
        products = body.get("products")
        if not isinstance(products, list):
            raise ReapMalformedResponse("Reap search response is missing products")
        return [self._candidate(product) for product in products if isinstance(product, dict)]

    async def get_product(self, product_id: str) -> dict:
        body = await self.client.request(
            "POST", "/agentic/products/details", json_body={"productIds": [product_id]}
        )
        products = body.get("products")
        if not isinstance(products, list) or not products or not isinstance(products[0], dict):
            raise ReapMalformedResponse("Reap details response did not include the requested product")
        return self._candidate(products[0])

    async def create_quote(
        self,
        variant_id: str,
        quantity: int,
        email: str,
        shipping_address: dict | None,
        idempotency_key: str,
    ) -> dict:
        if not isinstance(quantity, int) or quantity < 1:
            raise ValueError("quantity must be a positive integer")
        payload: dict[str, Any] = {
            "items": [{"variantId": variant_id, "quantity": quantity}],
            "email": email,
        }
        if shipping_address is not None:
            payload["shippingAddress"] = shipping_address
        body = await self.client.request(
            "POST", "/agentic/quotes", json_body=payload, idempotency_key=idempotency_key
        )
        return self._quote(body)

    async def get_quote(self, quote_id: str) -> dict:
        body = await self.client.request("GET", f"/agentic/quotes/{quote_id}")
        return self._quote(body)

    async def create_checkout(
        self, quote_id: str, enrollment_id: str, return_url: str, idempotency_key: str
    ) -> dict:
        headers: dict[str, str] = {}
        # Sandbox-only, opt-in configuration. Never infer this from the environment.
        if os.getenv("REAP_SIMULATE_CHECKOUT", "").strip() == "COMPLETED":
            headers["X-Simulate-Checkout"] = "COMPLETED"
        body = await self.client.request(
            "POST",
            "/agentic/checkouts",
            idempotency_key=idempotency_key,
            extra_headers=headers,
            json_body={
                "quoteId": quote_id,
                "enrollmentId": enrollment_id,
                "presentation": {"type": "REDIRECT", "returnUrl": return_url},
            },
        )
        return self._checkout(body)

    async def get_checkout(self, checkout_id: str) -> dict:
        body = await self.client.request("GET", f"/agentic/checkouts/{checkout_id}")
        return self._checkout(body)

    @staticmethod
    def _candidate(product: dict[str, Any]) -> dict:
        merchant = _as_object(product.get("merchant"))
        variant = _as_object(product.get("defaultVariant")) or _as_object(product.get("previewVariant"))
        merchant_name = merchant.get("name") if isinstance(merchant.get("name"), str) else None
        variant_id = variant.get("id") if isinstance(variant.get("id"), str) else None
        available_value = product.get("available", variant.get("available"))
        available = available_value if isinstance(available_value, bool) else None
        requires_shipping = product.get("requiresShipping")
        if not isinstance(requires_shipping, bool):
            requires_shipping = None
        return {
            "product_id": _nonempty_string(product.get("id"), "product id"),
            "name": product.get("name") if isinstance(product.get("name"), str) else None,
            "merchant_name": merchant_name,
            # Reap's current product response exposes a name, not a verified merchant ID.
            "merchant_key": merchant_name,
            "variant_id": variant_id,
            "available": available,
            "requires_shipping": requires_shipping,
            "raw_evidence": {
                "product_id": product.get("id"),
                "merchant_name": merchant_name,
                "variant_id": variant_id,
                "available": available,
                "requires_shipping": requires_shipping,
            },
        }

    @staticmethod
    def _quote(body: dict[str, Any]) -> dict:
        breakdown = _as_object(body.get("amountBreakdown"))
        total_minor, currency = _required_total_minor(breakdown.get("finalAmount"))
        selected = next(
            (
                option
                for option in body.get("shippingOptions", [])
                if isinstance(option, dict) and option.get("selected") is True
            ),
            None,
        )
        selected_object = _as_object(selected)
        return {
            "quote_id": _nonempty_string(body.get("id"), "quote id"),
            "total_minor": total_minor,
            "currency": currency,
            "expires_at": body.get("expiresAt") if isinstance(body.get("expiresAt"), str) else None,
            "subtotal_minor": _optional_amount_minor(breakdown.get("itemsSubtotal"), currency=currency),
            "shipping_minor": _optional_amount_minor(breakdown.get("shipping"), currency=currency),
            "tax_minor": _optional_amount_minor(breakdown.get("tax"), currency=currency),
            "selected_shipping": selected_object.get("name") if isinstance(selected_object.get("name"), str) else None,
            "raw_evidence": {
                "quote_id": body.get("id"),
                "currency": currency,
                "final_amount": breakdown.get("finalAmount"),
                "expires_at": body.get("expiresAt"),
            },
        }

    @staticmethod
    def _checkout(body: dict[str, Any]) -> dict:
        amount_breakdown = _as_object(body.get("amountBreakdown"))
        final_amount = body.get("finalAmount") or amount_breakdown.get("finalAmount")
        final_currency = _as_object(final_amount).get("currency")
        final_minor = _optional_amount_minor(final_amount, currency=final_currency) if final_amount else None
        status = _nonempty_string(body.get("status"), "checkout status")
        return {
            "checkout_id": _nonempty_string(body.get("id"), "checkout id"),
            "status": status,
            "next_action_url": _next_action_url(body),
            "order_id": body.get("orderId") if isinstance(body.get("orderId"), str) else None,
            "final_amount_minor": final_minor,
            "currency": final_currency if isinstance(final_currency, str) else None,
            "raw_evidence": {
                "checkout_id": body.get("id"),
                "status": status,
                "order_id": body.get("orderId"),
                "final_amount": final_amount,
            },
        }
