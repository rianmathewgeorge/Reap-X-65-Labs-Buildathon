"""Controlled, redacted smoke checks for a locally configured Reap sandbox account."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from app.reap_adapter import ReapAdapter
from app.reap_client import ReapError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one explicit Reap sandbox smoke-check stage.")
    parser.add_argument("--stage", required=True, choices=["enrollment", "search", "quote", "checkout", "status"])
    parser.add_argument("--enrollment-id")
    parser.add_argument("--checkout-id")
    parser.add_argument("--quote-id")
    parser.add_argument("--variant-id")
    parser.add_argument("--query", default="USB-C hub")
    parser.add_argument("--email")
    parser.add_argument("--shipping-address-file")
    parser.add_argument("--confirm-sandbox-operation", action="store_true")
    return parser


def _required(value: str | None, flag: str) -> str:
    if not value:
        raise ValueError(f"{flag} is required for this stage")
    return value


def _safe_id(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[:10]}…" if len(value) > 10 else value


def _read_address(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("shipping-address-file must contain a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("shipping-address-file must contain a JSON object")
    return data


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    adapter = ReapAdapter()
    try:
        if args.stage == "enrollment":
            return await adapter.get_enrollment(_required(args.enrollment_id, "--enrollment-id"))
        if args.stage == "search":
            results = await adapter.search_products(args.query)
            return {"count": len(results), "candidate": results[0] if results else None}
        if args.stage == "quote":
            if not args.confirm_sandbox_operation:
                raise ValueError("quote needs --confirm-sandbox-operation")
            return await adapter.create_quote(
                _required(args.variant_id, "--variant-id"),
                1,
                _required(args.email, "--email"),
                _read_address(_required(args.shipping_address_file, "--shipping-address-file")),
                str(uuid.uuid4()),
            )
        if args.stage == "checkout":
            if not args.confirm_sandbox_operation:
                raise ValueError("checkout needs --confirm-sandbox-operation")
            return await adapter.create_checkout(
                _required(args.quote_id, "--quote-id"),
                _required(args.enrollment_id, "--enrollment-id"),
                _required(os.getenv("REAP_RETURN_URL"), "REAP_RETURN_URL"),
                str(uuid.uuid4()),
            )
        return await adapter.get_checkout(_required(args.checkout_id, "--checkout-id"))
    finally:
        await adapter.client.aclose()


def _print_safe(result: dict[str, Any]) -> None:
    # Never print the raw evidence object: it is for server-side persistence only.
    safe = {key: value for key, value in result.items() if key not in {"raw_evidence", "next_action_url"}}
    for identifier in ("enrollment_id", "quote_id", "checkout_id", "order_id"):
        if identifier in safe:
            safe[identifier] = _safe_id(safe[identifier])
    candidate = safe.get("candidate")
    if isinstance(candidate, dict):
        safe["candidate"] = {
            key: candidate.get(key)
            for key in ("product_id", "name", "merchant_name", "variant_id", "available", "requires_shipping")
        }
    print(json.dumps(safe, indent=2, sort_keys=True, default=str))


def main() -> int:
    args = _parser().parse_args()
    try:
        _print_safe(asyncio.run(_run(args)))
    except (ValueError, ReapError) as exc:
        print(f"Smoke check stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
