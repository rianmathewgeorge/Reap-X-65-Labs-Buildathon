# CODEX BRIEF 2 - PANKAJ: SpendPilot Policy, Agent, UI and Checkout Guard

**Paste this complete file into your Codex session and also give Codex `00_SHARED_CONTRACT.md`.**
**Owner:** Pankaj. **Mission:** Deliver the understandable, genuinely usable SpendPilot product on top of Rian's Reap adapter. Your code alone has permission to initiate a checkout, after server-side authorization rules, attempt recording and budget reservation.

## Codex instructions: implement the MVP now

Read `00_SHARED_CONTRACT.md` first and inspect repository. Keep the frozen scope: single synthetic business, one supported SG merchant, USB-C hub procurement, SGD 100 per-checkout cap and SGD 200 overall run budget, one completed real sandbox checkout and one backend-blocked case. Implement code, tests, and a polished single-page UI. Do not expand to marketplace, inventory, bank payments, mandates, wallets, receipts, recurring automation or multiple agents. Do not edit Rian's files `app/reap_client.py`, `app/reap_adapter.py`, `scripts/reap_smoke.py`, or `tests/test_reap_adapter.py`.

Use an injectable `ReapAdapter` interface from the shared contract. Until Rian's branch is integrated, use a strictly in-process `FakeReapAdapter` in **test/demo fixture mode**, conspicuously marked `POLICY TEST - NOT LIVE REAP`. Never present fixture checkout success as a real sandbox purchase. Default production-style app behavior must not silently fall back to fixtures on API errors.

## Files you own

- `app/main.py`, `app/models.py`, `app/policy.py`, `app/storage.py`, `app/purchases.py`, `app/agent.py`, `app/passport.py`
- `templates/index.html`, `static/app.js`, `static/style.css`
- `tests/test_policy.py`, `tests/test_purchases.py`, `requirements.txt`, `.env.example`, `README.md`

## UX: one page, three stages

**Stage A: Confirm scope**
- Header: `SpendPilot | Equip a new teammate` and permanent `SANDBOX` label.
- Named synthetic customer: `Harbour Studio`; trusted backend configuration for business, owner/enrollment, market, merchant allowlist, product category and budget. No pretend KYB or verified business identity.
- Show `S$100 maximum per checkout`, `S$200 run budget`, `SGD`, `Singapore`, approved merchant chosen only after team-key confirmation.
- Input example: `Find one approved USB-C hub for our new employee, within S$100 total including delivery. Show me the quote before checkout.`
- User explicitly starts the run. Changes to scope require new run confirmation; NEVER let text or LLM change financial authority.

**Stage B: Agent search and approval**
- Show visible tool activity (`Searching Reap catalog`, `Checking variant`, `Getting final quote`, `Applying policy`, `Preparing checkout`). Don't show steps that didn't execute.
- Display actual product, verified merchant, quantity, quote total/breakdown, expiry and currency. Unknown product attributes are shown as unknown, not made up.
- Show policy checklist with `PASS / FAIL / UNKNOWN`; if blocked, show `No checkout created` **only if the server confirms no checkout POST took place**.
- If allowed, button `Prepare checkout & review on Reap`; this triggers a guarded server endpoint. It must never auto-run on refresh or autonomous agent continuation. Display returned hosted action as a user-initiated link only.

**Stage C: Outcome + Passport**
- On return from Reap, retrieve authoritative checkout status by its server-stored checkout ID. Return redirect does not mean payment success.
- Distinguish `REQUIRES_ACTION`, `PROCESSING`, `COMPLETED`, `FAILED`, `EXPIRED`, `UNKNOWN`.
- Show `Sandbox checkout completed` only for actual `COMPLETED`. Show returned `orderId` and actual `finalAmount` if present.
- Show a compact Purchase Decision Passport timeline: `USER_SCOPE -> REAP_RESPONSE -> APP_POLICY -> HOSTED_ACTION -> REAP_RESPONSE` with final outcome. Redact identifying data and secrets.

Accessible form labels, focus outlines, text statuses (not color-only), keyboard-supported approval link. Keep UI lightweight and premium: spacious typography, restrained monochrome plus one accent. Do not spend >40 minutes styling before successful integration.

## Policy engine (hard gates)

Create `evaluate_purchase(scope, candidate, quote, budget_snapshot, enrollment) -> PolicyDecision` with rule outcomes and evidence-source labels. A candidate can proceed only if ALL mandatory checks are VERIFIED/PASS:

1. `scope_confirmed` is true and not expired/changed.
2. Exact trusted business/enrollment owner mapping; enrollment must be ACTIVE.
3. Product ID + variant + requested quantity match previously trusted server-side selected request; quantity within configured scope (default 1).
4. Merchant matches trusted preconfigured allowlist using verified canonical identifier or confirmed exact merchant name, not LLM text.
5. Market is SG and quote currency is SGD.
6. Quote ID exists; `total_minor` is a valid, positive exact integer; `expires_at` not elapsed; quote is from Reap live response in real-run mode.
7. `total_minor <= per_checkout_cap_minor` and `total_minor <= available_minor` where available = run cap - completed actual charges - outstanding reservations.
8. No pending/unknown checkout for the same logical request/version, and no contradictory previous checkout identity.

Hard requirement that is `UNKNOWN` must fail closed. Rank eligible candidates only after hard constraints pass; MVP may select a single candidate. Do not promise hardware specifications are verified if API does not expose them. Show request requirement status honestly.

Critical: Backend not frontend or LLM owns budget, approved merchants, enrollment IDs, API host, return URL, and quote-to-product binding. The browser may send only request text, request ID, and explicit confirmation. Never accept an arbitrary client-supplied quote amount, enrollment ID or checkout URL as spending authority.

## Persistent DB with atomic reservation

SQLite tables (minimum; modify names if useful):

`scopes(scope_id, business_label, trusted_owner_id, enrollment_id, currency, market, merchant_key, approved_product_type, permitted_quantity, per_checkout_cap_minor, total_budget_minor, confirmed_at, version)`

`purchase_requests(request_id, scope_id, scope_version, user_text, product_id, variant_id, merchant_key, quantity, quote_id, quote_total_minor, quote_currency, quote_expires_at, state, created_at, updated_at)`

`checkout_attempts(attempt_id, request_id UNIQUE, idempotency_key UNIQUE, quote_id, enrollment_id, request_body_hash, reserved_minor, checkout_id UNIQUE, checkout_status, order_id, final_amount_minor, settlement_state, created_at, updated_at)`

`events(event_id, request_id, source_label, event_type, safe_payload_json, created_at)`

For a single-process buildathon app, enable SQLite busy timeout, WAL and `BEGIN IMMEDIATE` for atomic check/reservation/attempt insertion. Never hold a database lock during network calls. Assume one worker, no multi-replica deployment and disclose that limitation. A persisted attempt reservation must exist BEFORE external checkout creation.

Reservations count against total budget while outstanding (`RESERVED`, `REQUIRES_ACTION`, `PROCESSING`, `UNKNOWN`). On terminal confirmed failure/expiry, release exactly once. On `COMPLETED`, replace reservation with actual returned `finalAmount` exactly once. If final amount breaches a cap, show discrepancy and suspend further purchases; do not imply card charge was canceled or reversed.

If checkout POST times out and no checkout ID is known, keep `UNKNOWN` and the reservation. Do not create a new attempt/key or retry automatically. Reconcile when possible or use same idempotency key and unchanged payload according to documented protocol. Duplicated clicks return the same attempt. No background polling is necessary; explicit refresh/status lookup is fine.

## `app/purchases.py`: ONLY checkout entry point

Implement a single `async guarded_create_checkout(request_id, operator_confirmed=True) -> CheckoutAttempt` with this order:

1. Confirm explicit user interaction and trusted scope.
2. Load server-stored selected candidate, variant, quantity and quote ID. No client-controlled merchant, amount or enrollment.
3. Call `ReapAdapter.get_enrollment` for ACTIVE and owner scope; call `get_quote` to refresh quote total/currency/expiry. Ensure it still matches stored purchase/quote identity as far as exposed; if changed, invalidate previous eligibility and require review. Never claim Reap exposes fields it doesn't.
4. Evaluate all policy checks with current completed + outstanding balances. If blocked: persist APP_POLICY event, return error/blocked result, **do not call create_checkout**.
5. Atomically `BEGIN IMMEDIATE` recheck budget and unique request attempt, generate stable checkout idempotency key, persist attempt + reservation + exact planned checkout payload/hash, COMMIT.
6. Call adapter `create_checkout` once (outside DB transaction), passing the already persisted key and exact payload.
7. Persist returned `checkout_id`, Reap status and `next_action_url` (the URL can be held server-side and given to the browser only as needed; do not log or include in public Passport). Handle uncertain network errors as UNKNOWN with held reservation.
8. On hosted return, call `ReapAdapter.get_checkout` and reconcile the same attempt. Only a Reap COMPLETED status means completed sandbox transaction.

If `nextAction` is unexpectedly absent, do not claim approval happened; show actual status and escalate to organiser, since mandates are excluded.

## AI agent: minimal, observable and restricted

The AI is allowed to turn the user's request into a short search query and quantity/attributes **within scope**, call Reap search and quote tools, and explain the policy decision from actual evidence. Implement one LLM call + controlled tool calls instead of general multi-agent systems.

- Default to OpenAI API only when an API key/credits actually work. Put `OPENAI_API_KEY` and model selection in environment. Never send card/enrollment/API secrets, checkout URLs, addresses or private payment data to LLM.
- Validate model JSON output with a strict schema. Any product category, merchant, budget, quantity or currency inconsistent with trusted scope is rejected or clamped only when transparent; never silently escalate authority.
- Search with user keywords constrained to approved type. Use search candidate merchant data, not LLM hallucinations.
- If OpenAI credits are not available, use a clearly labeled `RULE-BASED DEMO INPUT` fallback for the single example rather than claiming an LLM actually ran. Real Reap API calls must remain genuine.
- Show observable tool operations that actually executed, not illustrative fake chains in live mode.

## App JSON route contract

Implement these **app** endpoints, not direct Reap proxy endpoints:

- `GET /api/health` -> `{status: "ok", mode: "live|policy_test"}`.
- `GET /api/scope` -> trusted scope, remaining budget, enrollment readiness (no secrets).
- `POST /api/requests` body `{text: str}` -> `{request_id, scope_summary, state:"DRAFT"}`. Confirmation of scope is handled in the UI/server session; don't trust client budgets.
- `POST /api/requests/{id}/discover` -> `{state, candidate, quote, rule_results, tool_events}` (actual live or labeled policy-test).
- `POST /api/requests/{id}/checkout` body `{confirm: true}` -> either `{state:"BLOCKED", reasons, checkout_created:false}` OR `{state:"REQUIRES_ACTION|PROCESSING|...", checkout_id, next_action_url, checkout_created:true}`. Requires real local operator confirmation and a same-origin demo guard; never have the LLM autonomously trigger this.
- `GET /api/requests/{id}` -> latest persisted record/state; refresh is read-only.
- `GET /api/requests/{id}/passport` -> ordered source-labeled evidence events, safely redacted.
- `GET /payment/return` -> lookup a previously stored attempt by safe state/session correlation, reconcile actual status, render/redirect to app status; never rely on query parameters alone to claim success.

For web security, use same-origin requests and a simple session-specific CSRF token or equivalent authorization check on checkout creation; CORS must not be open to arbitrary sites. Restrict to single trusted demo user/session, not pretend a label proves ownership.

## Minimum tests (no live API in pytest)

- Trusted cap 10000 cents permits quote 7290 and rejects 10001; 10000 is allowed.
- Trusted run cap 20000 accounts for completed + outstanding reservations; reservation prevents double-spend.
- Failed or unknown merchant/market/currency/variant/availability/amount/expiry fails closed.
- Missing quote total or malformed fractional minor-unit amount fails closed.
- LLM-injected budget/merchant/enrollment changes cannot alter trusted scope.
- For blocked policy, spy adapter shows **zero calls** to `create_checkout`.
- A double click or refresh does not cause a duplicate checkout POST.
- Timeout during checkout keeps UNKNOWN reservation; later status lookup handles completion/failure accurately.
- Hosted return does not mark complete until `get_checkout` returns COMPLETED.
- Passport includes REAP_RESPONSE and APP_POLICY evidence separately and has no secrets.
- Synthetic fixture mode clearly says `POLICY TEST`, never `Sandbox checkout completed` unless real Reap returned COMPLETED.

## Time boxes / completion

- 0-20 min: repo scaffold, scope model, mock adapter matching shared signatures, core routes.
- 20-60 min: implement tested policy engine and atomic reservations, blocked-case UI.
- 60-110 min: requests/discover orchestration + AI (if available), guarded checkout integration interface.
- 110-155 min: full UI and Decision Passport, status states.
- 155-190 min: merge Rian adapter; jointly resolve mapping and checkout behavior.
- 190+ min: testing, bugs, documentation, evidence and submission. Freeze by 8:00 PM and target 8:30 PM submission.

## Acceptance criteria and handoff

Must show one **real** (not fixture) Reap sandbox discovery, quote and checkout ending in authoritative COMPLETED if enabled; one server BLOCKED request with zero checkout calls; one understandable Passport per attempt; no secrets in UI or LLM; and verified duplicate/unknown handling. If a live payment integration is blocked, show accurate error and clearly labelled fixture evidence rather than claiming success.

When done, report: files changed, tests, app run command, fixture-mode caveats, known API blockers, screenshots/evidence path if produced, and exactly what Rian must connect. Do not overstate customer validation, compliance or real deliveries.
