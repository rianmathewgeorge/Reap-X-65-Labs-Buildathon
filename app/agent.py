from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from app.models import RestrictedIntent
from app.reap_client import ReapAPIError


class DiscoveryError(ValueError):
    def __init__(self, detail: str, events: list[dict[str, str]]) -> None:
        super().__init__(detail)
        self.events = events


class FakeReapAdapter:
    """In-process policy fixture, selected only when policy_test is explicit."""

    def __init__(self, merchant_key: str = "fixture-approved-merchant") -> None:
        self.merchant_key = merchant_key
        self.create_checkout_calls = 0
        self.checkouts: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.checkout_quotes: dict[str, str] = {}

    async def get_enrollment(self, enrollment_id: str) -> dict[str, Any]:
        return {"enrollment_id": enrollment_id, "status": "ACTIVE", "owner_id": "trusted-owner", "next_action_url": None}

    async def create_external_enrollment(self, owner_id: str, owner_email: str, return_url: str, idempotency_key: str) -> dict[str, Any]:
        return await self.get_enrollment("fixture-enrollment")

    async def search_products(self, query: str, country: str = "SG", currency: str = "SGD") -> list[dict[str, Any]]:
        return [{"product_id": "fixture-usbc-hub", "name": "USB-C Hub", "merchant_name": "Fixture Approved Merchant",
                 "merchant_key": self.merchant_key, "variant_id": None, "available": True, "requires_shipping": None, "raw_evidence": {}}]

    async def get_product(self, product_id: str) -> dict[str, Any]:
        return {"product_id": product_id, "name": "USB-C Hub", "merchant_name": "Fixture Approved Merchant",
                "merchant_key": self.merchant_key, "variant_id": "fixture-usbc-hub-default", "available": True,
                "requires_shipping": None, "raw_evidence": {}}

    async def create_quote(self, variant_id: str, quantity: int, email: str, shipping_address: dict | None, idempotency_key: str) -> dict[str, Any]:
        quote = {"quote_id": f"fixture-quote-{idempotency_key[-12:]}", "total_minor": 7290, "currency": "SGD",
                 "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "subtotal_minor": 7290,
                 "shipping_minor": None, "tax_minor": None, "selected_shipping": None, "raw_evidence": {}}
        self.quotes[quote["quote_id"]] = quote
        return quote

    async def get_quote(self, quote_id: str) -> dict[str, Any]:
        if quote_id not in self.quotes:
            raise ValueError("fixture quote is unknown")
        return self.quotes[quote_id]

    async def create_checkout(self, quote_id: str, enrollment_id: str, return_url: str, idempotency_key: str) -> dict[str, Any]:
        self.create_checkout_calls += 1
        if quote_id not in self.quotes:
            raise ValueError("fixture quote is unknown")
        checkout = {"checkout_id": f"fixture-checkout-{idempotency_key[-12:]}", "status": "REQUIRES_ACTION", "next_action_url": None,
                    "order_id": None, "final_amount_minor": None, "currency": "SGD", "raw_evidence": {}}
        self.checkouts[checkout["checkout_id"]] = checkout
        self.checkout_quotes[checkout["checkout_id"]] = quote_id
        return checkout

    async def get_checkout(self, checkout_id: str) -> dict[str, Any]:
        return self.checkouts.get(checkout_id, {"checkout_id": checkout_id, "status": "UNKNOWN", "next_action_url": None,
                                                "order_id": None, "final_amount_minor": None, "currency": "SGD", "raw_evidence": {}})

    def set_checkout_outcome(self, checkout_id: str, outcome: str) -> None:
        quote_id = self.checkout_quotes.get(checkout_id)
        if quote_id is None or checkout_id not in self.checkouts:
            raise ValueError("fixture checkout is unknown")
        quote = self.quotes[quote_id]
        checkout = self.checkouts[checkout_id]
        if outcome == "COMPLETED":
            checkout.update(status="COMPLETED", next_action_url=None, order_id=f"fixture-order-{checkout_id[-12:]}",
                            final_amount_minor=quote["total_minor"], currency=quote["currency"])
        elif outcome == "FAILED":
            checkout.update(status="FAILED", next_action_url=None, order_id=None, final_amount_minor=None, currency=quote["currency"])
        else:
            raise ValueError("fixture outcome is invalid")


def safe_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: candidate.get(key) for key in ("product_id", "name", "merchant_name", "merchant_key", "variant_id", "available", "requires_shipping")}


def safe_quote(quote: dict[str, Any]) -> dict[str, Any]:
    return {key: quote.get(key) for key in ("quote_id", "total_minor", "currency", "expires_at", "subtotal_minor", "shipping_minor", "tax_minor")}


def _intent_from_response(response: dict[str, Any]) -> RestrictedIntent:
    text = response.get("output_text")
    if not isinstance(text, str):
        for item in response.get("output", []):
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    text = content["text"]
                    break
    if not isinstance(text, str):
        raise ValueError("model returned no JSON text")
    return RestrictedIntent.model_validate_json(text)


def _reject_outside_scope(text: str) -> None:
    normalized = text.lower()
    if "hub" not in normalized:
        raise ValueError("rule-based input must request the approved hub")
    if any(term in normalized for term in ("laptop", "monitor", "phone", "wallet", "mandate", "bank transfer", "subscription")):
        raise ValueError("this demo supports the approved USB-C hub category only")
    if any(term in normalized for term in ("ignore previous", "system prompt", "override policy", "change budget", "change merchant", "change enrollment", "api key")):
        raise ValueError("authority-changing instructions are outside the trusted scope")
    quantity = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twenty)\s+(?:approved\s+)?(?:usb[ -]?c\s+)?hubs?\b", normalized)
    if (quantity and quantity.group(1) not in {"1", "one"}) or (re.search(r"\bhubs\b", normalized) and not quantity):
        raise ValueError("trusted scope permits quantity one only")


async def _interpret_request(text: str, scope: dict[str, Any], events: list[dict[str, str]]) -> str:
    _reject_outside_scope(text)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        events.append({"tool": "interpret_request", "status": "fallback: rule-based demo input"})
        return "USB-C hub"
    safe_words = re.findall(r"[a-z]+", text.lower())
    safe_input = " ".join(word for word in safe_words if word in {"usb", "c", "hub", "hubs", "one", "approved", "new", "employee", "delivery", "quote", "ports", "hdmi", "ethernet"})
    payload = {"text": {"format": {"type": "json_schema", "name": "procurement_intent", "strict": True, "schema": {"type": "object", "properties": {"query": {"type": "string"}, "quantity": {"type": "integer", "enum": [1]}}, "required": ["query", "quantity"], "additionalProperties": False}}}, "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "input": [{"role": "system", "content": "Return JSON only: {\\\"query\\\": string, \\\"quantity\\\": integer}. Query must describe a USB-C hub. Quantity must be 1."}, {"role": "user", "content": safe_input}], "temperature": 0}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            response.raise_for_status()
        intent = _intent_from_response(response.json())
        _reject_outside_scope(intent.query)
        if intent.quantity != scope["permitted_quantity"] or not re.fullmatch(r"[A-Za-z0-9 ,+/-]+", intent.query) or "hub" not in intent.query.lower():
            raise ValueError("model intent is outside trusted scope")
        events.append({"tool": "interpret_request", "status": "executed: OpenAI restricted intent"})
        return intent.query
    except (httpx.HTTPError, ValidationError, ValueError, json.JSONDecodeError):
        events.append({"tool": "interpret_request", "status": "fallback: OpenAI unavailable or invalid"})
        return "USB-C hub"


def _shipping_address() -> dict[str, Any] | None:
    value = os.getenv("SPENDPILOT_SHIPPING_ADDRESS_JSON")
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("trusted shipping configuration must be a non-empty JSON object")
    return parsed


def _is_approved_type(candidate: dict[str, Any], scope: dict[str, Any]) -> bool:
    name = candidate.get("name")
    return isinstance(name, str) and "hub" in name.lower() and "usb" in name.lower()


def _shipping_address_rejected(error: ReapAPIError) -> bool:
    if error.status != 400 or error.code != "AGENTIC_REQUEST_REJECTED" or not isinstance(error.detail, dict):
        return False
    errors = error.detail.get("errors")
    return isinstance(errors, list) and any(
        isinstance(item, dict)
        and isinstance(item.get("field"), str)
        and (item["field"] == "shippingAddress" or item["field"].startswith("shippingAddress."))
        for item in errors
    )


async def discover(adapter: Any, scope: dict[str, Any], text: str, request_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    """Only a narrowed search query may come from the optional model; authority stays server-side."""
    events: list[dict[str, str]] = []
    try:
        try:
            query = await _interpret_request(text, scope, events)
        except ValueError:
            raise DiscoveryError("request is outside the trusted single USB-C hub scope", events)
        if not isinstance(adapter, FakeReapAdapter):
            query = f"{scope['merchant_key']} {query}"
        events.append({"tool": "search_products", "status": "started"})
        candidates = await adapter.search_products(query, scope["market"], scope["currency"])
        events[-1]["status"] = "executed"
        selected = next((safe_candidate(item) for item in candidates if item.get("available") is True and item.get("merchant_key") == scope["merchant_key"] and item.get("product_id")), None)
        if selected is None:
            raise DiscoveryError("no available candidate matched the trusted merchant", events)
        events.append({"tool": "get_product", "status": "started"})
        detail = safe_candidate(await adapter.get_product(selected["product_id"]))
        events[-1]["status"] = "executed"
        if detail.get("product_id") != selected["product_id"] or detail.get("merchant_key") != scope["merchant_key"] or detail.get("available") is not True or not detail.get("variant_id"):
            raise DiscoveryError("product details did not verify the selected product, merchant, availability, and variant", events)
        if not _is_approved_type(detail, scope):
            raise DiscoveryError("product type is not an approved USB-C hub", events)
        shipping_required = detail.get("requires_shipping") is True or (
            detail.get("requires_shipping") is None and not isinstance(adapter, FakeReapAdapter)
        )
        shipping = _shipping_address() if shipping_required else None
        if shipping_required and shipping is None:
            raise DiscoveryError("shipping may be required; configure a trusted synthetic shipping address before quote creation", events)
        events.append({"tool": "create_quote", "status": "started"})
        try:
            quote = safe_quote(await adapter.create_quote(detail["variant_id"], scope["permitted_quantity"], f"demo+{request_id}@harbour-studio.invalid", shipping, str(uuid.uuid4())))
        except ReapAPIError as error:
            events[-1]["status"] = "failed"
            if _shipping_address_rejected(error):
                raise DiscoveryError("Reap rejected the configured shipping address; use organiser-provided sandbox shipping values before creating a new quote. No checkout was called.", events) from error
            raise
        events[-1]["status"] = "executed"
        return detail, quote, events
    except DiscoveryError:
        raise
    except Exception as error:
        if events and events[-1]["status"] == "started":
            events[-1]["status"] = "failed"
        raise DiscoveryError("Reap discovery or trusted shipping configuration could not be verified", events) from error
