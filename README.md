# SpendPilot

SpendPilot is the narrow Reap x 65labs sandbox demo for Harbour Studio: one trusted USB-C hub, one active enrollment, SGD 100 per checkout, and SGD 200 for the run. The server owns product binding, merchant identity, enrollment, amounts, budgets, idempotency, and checkout creation. It is not a marketplace, card vault, or autonomous buyer.

## Shared base and branches

Start both workstreams from shared commit `6faf1f0`.

- Rian's normalized `ReapAdapter` and controlled smoke tooling from `origin/rian` commit `fa662fd` are merged here.
- `feat/product-pankaj` owns this policy, UI, and server guard layer.

Do not commit `.env`, Reap keys, hosted approval URLs, raw response bodies, addresses, or card data.

## Install and run

Python 3.11+ is required. The local `.venv` is ignored by Git. To create a fresh environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For the clearly labelled local fixture demonstration:

```sh
SPENDPILOT_MODE=policy_test .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

Open `http://127.0.0.1:8000`. The fixture can produce an allowed quote and a `REQUIRES_ACTION` record, but it never calls Reap and never displays a completed sandbox order. Use the deliberate “two USB-C hubs” request for the backend-blocked demonstration.

Run checks with:

```sh
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app
node --test tests/frontend.test.cjs
```

## Live Reap handoff

The merged `app.reap_adapter.ReapAdapter` exposes the contract from `00_SHARED_CONTRACT.md`; the application calls only its normalized methods. Live use still needs the organiser-provided sandbox configuration below, set in the process environment and never committed:

```sh
SPENDPILOT_MODE=live
REAP_BASE_URL=...
REAP_API_KEY=...
REAP_VERSION=2025-02-14
REAP_ENROLLMENT_ID=...
REAP_MERCHANT_KEY=...
REAP_TRUSTED_OWNER_ID=...
REAP_RETURN_URL=http://127.0.0.1:8000/payment/return
```

If the Reap adapter or any required live setting is absent, live discovery fails closed. No fixture fallback is used for a live request. The optional OpenAI intent call is used only when `OPENAI_API_KEY` works; it may produce a restricted USB-C-hub search query, never merchant, budget, enrollment, quantity, or checkout authority. An invalid or unavailable model response uses the visibly labelled rule-based fallback.

For a shipping-required live candidate, configure `SPENDPILOT_SHIPPING_ADDRESS_JSON` as a trusted synthetic JSON address. It is used only for quote creation and is not stored in the purchase record, events, Passport, or browser.

## Checkout evidence and limits

- Every write uses the local session/CSRF guard and requires explicit human action. The user confirms the displayed scope before starting a run.
- A checkout reservation is written before the Reap call. `UNKNOWN`, `REQUIRES_ACTION`, and `PROCESSING` retain it. A redirect does not prove completion; the app fetches Reap's stored checkout status.
- The Passport contains only source-labelled, persisted safe events. It excludes hosted action URLs, API credentials, raw evidence, synthetic addresses, and payment details.
- SQLite reservation locking is suitable for the one-worker buildathon demo only. It needs shared transactional storage before multi-worker deployment.

The adapter currently uses Reap's exact returned merchant name as `merchant_key` when no stable merchant identifier is present. This is a weaker identity signal, so `REAP_MERCHANT_KEY` must be the exact canonical name confirmed by the sandbox record.

Current live blocker: no Reap credentials, confirmed sandbox host/version, enrollment, or canonical merchant mapping are configured in this environment. Do not claim sandbox search, checkout, or completion until organiser-provided credentials and Reap responses confirm them.

Each mode uses a separate database by default (`spendpilot-live.db` and `spendpilot-policy_test.db`). `SPENDPILOT_DB_PATH` may select another file. A changed trusted configuration requires a separate database and explicit new confirmation; never discard an existing live database to recover an uncertain checkout. Inspect retained attempts and use the same persisted operation/key only after Reap's retry protocol is confirmed. Fixture catalogs exist only in process memory; after restart, create a new fixture request rather than reusing an old quote.

The optional model receives only allowlisted procurement keywords, not raw user prose or private payment data. The displayed tool events report whether OpenAI ran or rule-based fallback was used. Missing hosted approval on an initial live checkout suspends new purchases until the organiser clarifies behavior.

`checkout_created: null` means a POST may have been submitted but the outcome is unknown. Only a backend-blocked result with `checkout_created: false` proves no checkout call occurred. Completed status and settlement are separate: a COMPLETED response without a valid actual SGD amount keeps its reservation and suspends purchases.

The local authorization guard rejects non-loopback clients, unexpected Host/Origin values, and POSTs without a server-known session and CSRF token. Do not expose this app publicly. The callback correlates an opaque stored state and always retrieves status through the adapter. If hosted redirects to loopback are unsupported by team setup, that is an integration blocker; this demo does not weaken its local guard.

With configured sandbox values, start with the read-only enrollment smoke check:

```sh
.venv/bin/python -m scripts.reap_smoke --stage enrollment --enrollment-id "$REAP_ENROLLMENT_ID"
```

Before joint validation, confirm the supported host/version, exact canonical merchant and trusted owner/enrollment mapping, synthetic shipping requirements, and a redacted real quote transcript. Run the full `pytest -q`, then perform hosted human approval and confirm COMPLETED via status lookup, followed by an independent blocked request.
