# Pankaj validation

Shared starting commit: `6faf1f03490f09972bc559ed8dad950093c32274`.
Branch: `feat/product-pankaj`.

- Python 3.14 local runtime; code targets Python 3.11+.
- `pytest -q`: 57 passed. One upstream Starlette TestClient deprecation warning; no live calls.
- Python compile check and JavaScript syntax check passed.
- HTTP `/api/health` observed `mode: policy_test`, `reap_ready: true` (fixture readiness).
- Browser: one S$72.90 fixture checkout returned REQUIRES_ACTION, reserved budget to S$127.10, and reload retained the single attempt.
- Browser: independent quantity-two request blocked before any external tool; persisted attempt count for blocked requests is zero.
- Desktop and 390px mobile rendered validation completed; document width equals viewport width at 390px.
- Screenshots: `policy-test-checkout.jpg`, `policy-test-blocked.jpg`, `policy-test-mobile.jpg`.
- `policy-test-records.json` contains synthetic IDs and redacted state/budget evidence only.

Live acceptance is outstanding. As checked during this run, Rian's remote `rian` branch still equals the shared base, and no `feat/reap-rian` branch is published. No Reap adapter or team credentials, merchant identity, trusted owner/enrollment mapping, or real quote are available here. No genuine sandbox payment, hosted approval, combined adapter suite, or live OpenAI call is claimed.

Preserve uncertain live databases. This local application supports one trusted operator, one worker, and loopback access only. It requires Rian's confirmed adapter constructor/configuration and actual responses before joint sandbox validation.

The supplied Homebrew instruction was attempted: `./bin/brew lgtm` cannot run because this repository has no `bin/brew`. Python/JavaScript checks above apply to this new application.
