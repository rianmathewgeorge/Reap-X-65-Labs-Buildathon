# CODEX BRIEF 1 - RIAN: Reap Integration Engineer

**Paste this complete file into your Codex session and also give Codex `00_SHARED_CONTRACT.md`.**
**Owner:** Rian. **Mission:** Provide the working, tested Reap Agentic Payments adapter so Pankaj can build a guarded purchasing application without reverse-engineering Reap responses.

## Codex instructions: execute, not just plan

You are implementing the Reap integration for SpendPilot in a shared repository. Read `00_SHARED_CONTRACT.md` first; its ownership and normalized function signatures are frozen. Inspect current repository before editing. Only modify files you own; do not rewrite `app/main.py`, `app/purchases.py`, `app/policy.py`, `app/storage.py`, `app/agent.py`, or frontend files. Write working code and tests. Do not invent endpoint names, payment status, approval URLs or credentials. Where account-specific requirements are unknown, use explicit environment config and fail with actionable errors. Never ask for or print raw PAN/CVV/API key. Do not automatically run a checkout on invocation or test suite; require explicit developer opt-in for any sandbox checkout smoke call. Do not modify or create mandates.

## Your exclusive files

- `app/reap_client.py`
- `app/reap_adapter.py`
- `scripts/reap_smoke.py`
- `tests/test_reap_adapter.py`
- `tests/fixtures/reap_*.json` (synthetic, redacted)

If Pankaj has already created `app/models.py`, import the shared normalized contract as suitable, without changing his files. Avoid dependencies on Pankaj's code for your own tests.

## Required Reap operations (implement this order)

1. `GET /agentic/enrollments/{id}` -> confirm `ACTIVE`; normalize owner and `nextAction`.
2. `POST /agentic/products/search` -> `query`, `context.country=SG`, `context.currency=SGD`, availability filter. Get product IDs and verified merchant data.
3. `POST /agentic/products/details` -> pass `productIds`; prefer a valid `defaultVariant`. If unsuitable, a variant-resolution helper may call `POST /agentic/products/variant` with product ID and available option IDs; don't expand UI scope.
4. `POST /agentic/quotes` -> exactly one `items` entry with `variantId`, `quantity`, customer email, shipping address if required; include persisted `Idempotency-Key`.
5. `GET /agentic/quotes/{id}` -> latest quote total/expiry; normalize selected shipping where provided.
6. `POST /agentic/checkouts` -> `quoteId`, server-trusted `enrollmentId`, `presentation` of type `REDIRECT`, `returnUrl`; include persisted checkout idempotency key. Sandbox simulation header only when explicitly configured and verified.
7. `GET /agentic/checkouts/{id}` -> authoritative checkout status, hosted `nextAction.url` where given, order ID and final amount if returned.
8. Conditional setup only: `POST /agentic/enrollments`, using `EXTERNAL` and hosted redirect for the organiser-provided test card if an active enrollment is not supplied. Card entry must occur exclusively on Reap's page. Retain enrollment ID and confirm ACTIVE after return.

Official documentation: https://docs.reap.global/agentic-payments/setup ; https://docs.reap.global/agentic-payments/one-time-purchases ; https://docs.reap.global/agentic-payments/lifecycle . API endpoint reference takes precedence over memory.

## HTTP requirements

- Async `httpx.AsyncClient` with timeouts, bounded connections and transport injection for mocked tests.
- Configure `REAP_BASE_URL`, `REAP_API_KEY`, `REAP_VERSION`, `REAP_SIMULATE_CHECKOUT`, `REAP_RETURN_URL` via environment. Don't silently pick a sandbox host if the team key has not confirmed it.
- Every request: `Authorization: Bearer ...`, `Reap-Version: ...`; JSON `Content-Type` on POST.
- `Idempotency-Key` for enrollment/quote/checkout creation, passed in by caller and **not generated inside the adapter**. Do not reuse across different operations or changed payloads.
- Server should never log credential-bearing headers, full customer email or full shipping address. Do not give card data to AI.
- Parse error `{error:{code,message,detail}}` into a typed `ReapAPIError(code,status,message,detail)` with safe string representation. Distinguish malformed responses, timeouts, 401/403, expiry, failed merchant quote and uncertain checkout call.
- No automatic retries on non-idempotent POST that could create a payment. For uncertain checkout POST, surface an outcome-unknown exception to Pankaj so his workflow retains its reservation. If explicitly retried later, must be same idempotency key and unchanged payload.
- Never mark checkout complete on HTTP 200 alone. Only Reap's `status == COMPLETED` is final success.

## `ReapAdapter` normalized return shape (contract)

Implement exactly the class signatures specified in `00_SHARED_CONTRACT.md`. Return shape examples:

```python
await adapter.get_enrollment("enr_test")
# {"enrollment_id":"enr_test", "status":"ACTIVE", "owner_id":"...", "next_action_url":None}

await adapter.search_products("USB-C hub", "SG", "SGD")
# [{"product_id":"...", "name":"...", "merchant_name":"...", "merchant_key":"...",
#   "variant_id":"... or None", "available":True, "requires_shipping":None or True, "raw_evidence":{...}}]

await adapter.get_product("prod_test")
# {"product_id":"...", "name":"...", "merchant_name":None or "...", "merchant_key":None or "...",
#   "variant_id":"default-variant or None", "available":True, "requires_shipping":True, "raw_evidence":{...}}

await adapter.create_quote("var_test", 1, "test@example.com", shipping_address, "quote-key")
# {"quote_id":"...", "total_minor":7290, "currency":"SGD", "expires_at":"...",
#   "subtotal_minor":..., "shipping_minor":..., "tax_minor":..., "selected_shipping":..., "raw_evidence":{...}}

await adapter.get_quote("quote_test")
# same quote schema

await adapter.create_checkout("quote_test", "enr_test", "https://.../payment/return", "checkout-key")
# {"checkout_id":"...", "status":"REQUIRES_ACTION", "next_action_url":"https://...",
#   "order_id":None, "final_amount_minor":None, "currency":"SGD", "raw_evidence":{...}}

await adapter.get_checkout("checkout_test")
# same checkout schema, real terminal fields populated only when present
```

A search result may not reliably supply product condition/specification; leave it unknown. A quote may not echo product identity; Pankaj will persist `quote_id -> product/variant/quantity` in app storage. Preserve actual native IDs. Do not manufacture a merchant ID if Reap returns only its name. `raw_evidence` means a minimal safe subset of the original response, not full private data.

### Money conversion

Do not assume cents in native Reap responses. Normalize Reap native numeric `amount` into integer minor SGD units using `Decimal(str(value))`, multiply by 100, require exact integer cents and USD/other currencies only if contract extended explicitly. Reject malformed, nonpositive total, missing currency, NaN/Inf, excess fractional cents. For optional shipping/tax/subtotal, allow missing/zero as appropriate. Never sum breakdown fields to reconstruct final amount; `amountBreakdown.finalAmount` is authoritative for the budget check. `tax.includedInPrices` is separate display metadata if present.

## Recommended smoke-test script stages

The script `python -m scripts.reap_smoke --stage search` should run only read/search operations. Stages: `enrollment`, `search`, `quote`, `checkout`, `status`. For quote or checkout stages require an explicit confirmation CLI flag (for example `--confirm-sandbox-operation`) and never auto-execute through pytest. Use IDs from actual previous steps, not invented IDs; store safe IDs only locally. Do not print request headers, personal shipping details, hosted URL query strings or payment-card details. Print only redacted IDs, status, currency, amount, quote expiry and error code.

The checkout flow may return `REQUIRES_ACTION` and `nextAction.url`. It must be returned to Pankaj's UI verbatim for **direct user navigation only**; do not fabricate or auto-visit it. Enrollment setup similarly requires the hosted page and subsequent ACTIVE lookup. If `nextAction` is unexpectedly null, report and ask organiser about approval behavior. Never claim approval occurred without evidence.

## Tests you must implement (mocked HTTP, no external API)

- Headers include bearer, `Reap-Version`, idempotency for creation; no idempotency for read.
- Search and product details normalise default variant, merchant and availability.
- Quote `finalAmount` -> exact minor units; nested tax-included handling; no double-counting.
- Quote expiry preserved and invalid amounts rejected.
- Checkout `REQUIRES_ACTION`, `PROCESSING`, `COMPLETED`, `FAILED`, `EXPIRED` mapped accurately.
- Timeout during checkout POST does not become failure or success automatically.
- API errors preserve safe error code/status without secrets.
- `create_checkout` is not automatically invoked by import, pytest or search stage.

## Time boxes

- First 10 min: establish env, host/version, credentials readiness (do not paste secrets).
- By 35 min: enrollment read + product search, details.
- By 60 min: quote works for a genuine variant/market/shipping context.
- By 90 min: controlled checkout/approval/status path is understood and tested; notify teammate of blockers earlier.
- After first live quote: give Pankaj normalized outputs + adapter import path so integration can happen immediately.

## Done / handoff message to Pankaj

Provide exactly:
1. Adapter import and constructor usage.
2. Supported operations and normalized field examples **from redacted actual responses** versus synthetic fixtures (label them).
3. Confirmed base host, API version and enrollment status (never actual key/card details).
4. Quote and checkout status evidence, without leaking tokens or hosted URL contents.
5. Test command and count/results.
6. Blocking API issues and any undocumented behavior.

**DO NOT** write a fake successful checkout fallback. If integration is blocked, report the real blocker and keep the tests green with explicitly synthetic response fixtures.
