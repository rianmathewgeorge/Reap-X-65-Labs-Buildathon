# SpendPilot - Shared Engineering Contract (FREEZE v1)

**Event:** Reap x 65labs Agentic Buildathon, 9 October 2026 (SGT)
**Hard deadline:** 9:00 PM SGT. **Internal submit target:** 8:30 PM SGT.
**Builders:** Rian (Reap integration) and Pankaj (policy, agent, web UI).
**Target:** One live Reap **sandbox** USB-C hub checkout, after Reap-hosted user approval; one independently BLOCKED purchase before checkout creation; one evidence-backed Purchase Decision Passport for each attempt.

READ THIS ENTIRE DOCUMENT BEFORE CODEX STARTS. This contract is authoritative for both branches. Do not expand scope.

## 1. Frozen scope and business rules

- One synthetic business: `Harbour Studio`, one synthetic owner/authorizer, one new-starter equipment procurement job.
- Singapore / SGD, one confirmed supported merchant, one USB-C hub type, one active enrollment associated with a trusted owner. Product availability and final quote **must** come from Reap, never hardcoded.
- Per-checkout cap: SGD 100.00 (10,000 cents). Total demo-run budget: SGD 200.00 (20,000 cents). Values are defaults; change them explicitly in the trusted demo scope **before run confirmation** if necessary to support a real verified quote.
- Quantity 1 by default; product, variant, allowed quantity, merchant, market and currency must match confirmed scope.
- A server-side policy gate is required; LLM must never modify scope, funding identity or authorization.
- Checkout only through Reap Agentic; no custom checkout URL, no raw card information handled by app, no mandates, no Kwal, no web scraping, no unsupported bank transfers.
- User approves the charge via Reap's `nextAction.url` when provided. A return redirect is not payment confirmation.
- The Purchase Decision Passport is an app-generated evidence timeline from persisted data, clearly labeled `USER_SCOPE`, `REAP_RESPONSE`, `APP_POLICY`, or `POLICY_TEST`. Never confuse a local test fixture with a live Reap transaction.

## 2. Stack and file ownership (avoid merge conflicts)

Use Python 3.11+, FastAPI, Pydantic v2, `httpx`, built-in `sqlite3`, and server-rendered HTML + vanilla JS (or use a pre-existing working stack without rewriting it).

**Rian exclusively owns:**
- `app/reap_client.py` - HTTP transport, Reap headers, retries/error mapping.
- `app/reap_adapter.py` - stable normalized interface described below.
- `scripts/reap_smoke.py` - controlled CLI smoke check; no unsafe card data.
- `tests/test_reap_adapter.py` - mocked Reap response tests.
- `tests/fixtures/reap_*.json` - **sanitized, synthetic** response fixtures only.

**Pankaj exclusively owns:**
- `app/main.py` - FastAPI app and routes.
- `app/policy.py` - fail-closed policy engine.
- `app/storage.py` - SQLite transactions, attempts, budget reservations, events.
- `app/purchases.py` - the **only application function permitted to call** `ReapAdapter.create_checkout()`.
- `app/agent.py` - LLM interpretation and restricted orchestration.
- `app/passport.py` - evidence-backed passport projection.
- `app/models.py` - app request/response Pydantic models.
- `templates/index.html`, `static/app.js`, `static/style.css` - UI.
- `tests/test_policy.py`, `tests/test_purchases.py`.
- `requirements.txt`, `.env.example`, README, application wiring.

**No shared editing.** Only one owner edits a file. Do not auto-reformat or rewrite the teammate's files. Import `ReapAdapter` lazily or use a stub behind the identical contract until integration is ready.

**Git:** Work from one common base commit in separate branches/worktrees (`feat/reap-rian` and `feat/product-pankaj`). Cherry-pick or merge Rian's tested integration into the product branch, resolve dependencies, then jointly test. Never commit `.env`.

## 3. Stable adapter contract (both Codex sessions MUST follow this)

Rian exports `class ReapAdapter` from `app/reap_adapter.py` with:

```python
class ReapAdapter:
    async def get_enrollment(self, enrollment_id: str) -> dict: ...
    async def create_external_enrollment(self, owner_id: str, owner_email: str,
                                         return_url: str, idempotency_key: str) -> dict: ...
    async def search_products(self, query: str, country: str = "SG",
                              currency: str = "SGD") -> list[dict]: ...
    async def get_product(self, product_id: str) -> dict: ...
    async def create_quote(self, variant_id: str, quantity: int,
                           email: str, shipping_address: dict | None,
                           idempotency_key: str) -> dict: ...
    async def get_quote(self, quote_id: str) -> dict: ...
    async def create_checkout(self, quote_id: str, enrollment_id: str,
                              return_url: str, idempotency_key: str) -> dict: ...
    async def get_checkout(self, checkout_id: str) -> dict: ...
```

Return normalized application dicts with **these exact snake_case fields**. Fields absent from the Reap response remain `None` or missing; **do not manufacture values**.

```json
{
  "enrollment": {"enrollment_id":"enr_...", "status":"ACTIVE", "owner_id":"trusted-owner", "next_action_url":null},
  "candidate": {"product_id":"prod_...", "name":"USB-C Hub", "merchant_name":"Verified merchant", "merchant_key":"verified exact name or stable id/domain", "variant_id":"var_...", "available":true, "requires_shipping":true, "raw_evidence":{}},
  "quote": {"quote_id":"quote_...", "total_minor":7290, "currency":"SGD", "expires_at":"2026-10-09T...Z", "subtotal_minor":null, "shipping_minor":null, "tax_minor":null, "selected_shipping":null, "raw_evidence":{}},
  "checkout": {"checkout_id":"check_...", "status":"REQUIRES_ACTION", "next_action_url":"https://...", "order_id":null, "final_amount_minor":null, "currency":"SGD", "raw_evidence":{}}
}
```

The four top-level example keys above identify four **separate return shapes** (not one combined response). Normalization logic maps the native Reap response to its one corresponding object. `raw_evidence` must be minimized/redacted and safe to persist/display; do not include personal address, card information, API key, or customer email. Never expose entire raw Reap bodies in frontend logs.

- `search_products` returns a **list of candidate dicts**; `get_product` returns one candidate/detail dict. Resolve `defaultVariant` if valid; if unavailable, report that option selection is not supported in MVP or implement variant resolution privately, keeping the same candidate schema.
- `quote.total_minor` must originate from `amountBreakdown.finalAmount.amount`. Reap example values are major-currency decimal amounts; verify conventions with live responses. For SGD convert using `Decimal(str(amount)) * 100` and reject non-integral cents, negative/zero, NaN, unsupported currency or malformed amounts. Do NOT use floats for financial comparisons or manually add tax to `finalAmount`.
- `merchant_key` should be a trusted canonical merchant identifier where provided. If only a name is returned, use an exact canonical name from the verified merchant/sandbox record, and explicitly document the weaker identity signal. NEVER trust the LLM to approve merchant identity.
- `get_quote()` might not return full product binding. Pankaj must keep the server's original `quote_id -> variant_id + quantity + product + merchant` association, re-check current quote ID, amount, currency and expiry and **not claim any field was re-verified if not exposed by Reap**.
- `get_checkout()` must accurately preserve `REQUIRES_ACTION`, `PROCESSING`, `COMPLETED`, `FAILED`, `EXPIRED`, or unknown response. Only `COMPLETED` means completed sandbox order.
- Do not use an unguarded public checkout route, even for convenience. Rian may exercise create_checkout from a controlled local smoke script only after explicitly confirming intent to run sandbox checkout.

## 4. Application routes (Pankaj implements)

These are **our app routes**, NOT Reap API endpoints:

| Method | Path | Result |
|---|---|---|
| GET | `/api/scope` | Trusted demo policy and remaining budget |
| POST | `/api/requests` | Accept request text; create persisted request/run; no checkout |
| POST | `/api/requests/{id}/discover` | Agent searches and prepares candidate/quote via `ReapAdapter`; no checkout |
| GET | `/api/requests/{id}` | Current attempt and evidence timeline |
| POST | `/api/requests/{id}/checkout` | **Guarded** evaluation/reservation/checkout, no bypass |
| GET | `/api/requests/{id}/passport` | Source-labeled decision record |
| GET | `/payment/return` | Get status; never assume redirect == completion |
| GET | `/api/health` | Nonsecret readiness |

Use structured JSON responses, documented error codes, consistent statuses. A `checkout` request contains at most `request_id` and user confirmation flag; the backend loads quote/merchant/budget/enrollment from persisted trusted state. CSRF/session or a local demo authorization guard must prevent arbitrary unauthenticated external checkout creation. All sensitive actions require explicit human interaction.

## 5. Budget, reservation and identity invariant

- A unique `request_id` identifies a user-approved request/version, and one persisted checkout attempt uses one stable UUID idempotency key with an unchanged request body.
- Use `BEGIN IMMEDIATE` in SQLite to atomically verify available run budget and persist a reservation and attempt before calling Reap. **Do not hold the SQLite transaction open during network calls.** Single-instance/one-worker demo only; document that limitation.
- Available budget = total cap - completed actual debits - outstanding reservations. Per-checkout cap also checked. Reservation stays while checkout is `REQUIRES_ACTION`, `PROCESSING`, or `UNKNOWN`.
- Terminal `FAILED` or `EXPIRED` or verified non-submission may release reservation once. `COMPLETED` reconciles reserved amount with Reap `finalAmount` once. If mismatch breaches cap, flag discrepancy and stop new work; do not claim reversal.
- `UNKNOWN` keeps reservation; no new idempotency key or second checkout during uncertainty. Retry only same operation with same key/payload if safe under Reap's documented 24h window and after inspecting existing state.
- Refresh never performs a POST checkout automatically. Duplicate clicks return the same attempt/record.
- The agent cannot override approved merchant, item, quantity, enrollment, owner, currency or budget.

## 6. Minimum demo acceptance tests

1. Real sandbox search returns a candidate and quote (or exact documented blocker).
2. Checkout requires ACTIVE enrollment and a quote not expired.
3. Allowed purchase creates one checkout; user opens actual `nextAction.url` if returned.
4. Redirect causes GET checkout; never treat redirect alone as successful payment.
5. `COMPLETED` shows sandbox-only message, order reference and real returned amount.
6. Any over-budget or unapproved merchant attempt is `BLOCKED` and no `POST /agentic/checkouts` is invoked.
7. Two concurrent/double-click calls on same request create at most one checkout attempt.
8. Unknown/timeout retains reservation and blocks accidental repeat.
9. Passport labels `REAP_RESPONSE` vs `APP_POLICY` vs `USER_SCOPE` vs `POLICY_TEST`.
10. Secrets/card data never appear in code, requests to LLM, public logs or screenshots.

## 7. Verified documentation and unresolved questions

Official: https://docs.reap.global/agentic-payments/overview
Setup: https://docs.reap.global/agentic-payments/setup
Purchase: https://docs.reap.global/agentic-payments/one-time-purchases
Lifecycle: https://docs.reap.global/agentic-payments/lifecycle
API reference: https://docs.reap.global/api-reference/agentic/create-quote and https://docs.reap.global/api-reference/agentic/create-checkout

The walkthrough uses `https://sandbox.api.reap.global`; the detailed API reference uses `https://sg.sandbox.api.reap.global`. Treat `REAP_BASE_URL` as an environment setting and confirm which host the team key supports. All calls use bearer auth and `Reap-Version`, version per account/docs (API reference currently shows `2025-02-14`). Enrollment, quotes and checkout creation use unique per-operation `Idempotency-Key` values. Checkout sandbox simulation may use `X-Simulate-Checkout: COMPLETED` **only if allowed by team setup**.

The one-time guide references mandates, while participant instructions indicate they are unavailable. No mandate endpoints in this MVP. If the checkout unexpectedly skips hosted approval, display real state and stop until approved-path behavior is clarified. Do not invent a hosted approval link.

## 8. Hand-off rules (mandatory)

- Rian provides **normalized function outputs** and a controlled, redacted smoke-test transcript, plus exact caveats. Pankaj integrates via `ReapAdapter` only.
- Pankaj provides all app routes/DB policy behavior and a UI running with clearly labeled fixture mode before merge; no fabricated payment success.
- Each Codex session must run its own tests and report tests not run, files changed, blockers, and integration instructions.
- Merge at the first live quote (not after both branches are perfect). Jointly test one approved and one blocked case. Freeze features by 8:00 PM; submit by 8:30 PM.
