from __future__ import annotations

from typing import Any


def project_passport(request: dict[str, Any], events: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """Passport intentionally projects persisted safe events only; hosted URLs never enter it."""
    return {"request_id": request["request_id"], "state": request["state"], "mode": mode,
            "events": events, "fixture_notice": "POLICY TEST - NOT LIVE REAP" if mode == "policy_test" else None}
