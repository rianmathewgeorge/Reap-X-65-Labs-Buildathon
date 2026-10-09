from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Storage:
    """Single-process SQLite storage; run exactly one application worker."""

    def __init__(self, path: str = "spendpilot.db") -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.Lock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS scopes (
              scope_id TEXT PRIMARY KEY, business_label TEXT NOT NULL, trusted_owner_id TEXT NOT NULL,
              enrollment_id TEXT NOT NULL, currency TEXT NOT NULL, market TEXT NOT NULL, merchant_key TEXT NOT NULL,
              approved_product_type TEXT NOT NULL, permitted_quantity INTEGER NOT NULL,
              per_checkout_cap_minor INTEGER NOT NULL, total_budget_minor INTEGER NOT NULL,
              confirmed_at TEXT NOT NULL, version INTEGER NOT NULL, suspended INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS purchase_requests (
              request_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(scope_id), scope_version INTEGER NOT NULL,
              user_text TEXT NOT NULL, product_id TEXT, variant_id TEXT, merchant_key TEXT, quantity INTEGER,
              quote_id TEXT, quote_total_minor INTEGER, quote_currency TEXT, quote_expires_at TEXT,
              candidate_json TEXT, quote_json TEXT, source_mode TEXT, state TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkout_attempts (
              attempt_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL REFERENCES purchase_requests(request_id),
              idempotency_key TEXT UNIQUE NOT NULL, quote_id TEXT NOT NULL, enrollment_id TEXT NOT NULL,
              request_body_hash TEXT NOT NULL, request_body_json TEXT NOT NULL, reserved_minor INTEGER NOT NULL,
              checkout_id TEXT UNIQUE, checkout_status TEXT NOT NULL, settlement_state TEXT NOT NULL,
              order_id TEXT, final_amount_minor INTEGER, next_action_url TEXT, return_state_hash TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES purchase_requests(request_id),
              source_label TEXT NOT NULL, event_type TEXT NOT NULL, safe_payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)

    def seed_scope(self, scope: dict[str, Any]) -> None:
        existing = self.connection.execute("SELECT * FROM scopes LIMIT 1").fetchone()
        if existing:
            if any(existing[field] != value for field, value in scope.items() if field != "confirmed_at"):
                raise RuntimeError("trusted scope changed; use a separate demo database and confirm its scope")
            return
        self.connection.execute("INSERT INTO scopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)", tuple(scope.values()))

    def scope(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM scopes LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("trusted demo scope is missing")
        return dict(row)

    def budget_snapshot(self) -> dict[str, int]:
        row = self.connection.execute("""SELECT
              COALESCE(SUM(CASE WHEN settlement_state IN ('SETTLED','DISCREPANCY') THEN final_amount_minor ELSE 0 END), 0) AS completed_minor,
              COALESCE(SUM(CASE WHEN settlement_state='HELD' THEN reserved_minor ELSE 0 END), 0) AS reserved_minor
              FROM checkout_attempts""").fetchone()
        completed, reserved = int(row["completed_minor"]), int(row["reserved_minor"])
        cap = self.scope()["total_budget_minor"]
        return {"total_budget_minor": cap, "completed_minor": completed, "reserved_minor": reserved, "available_minor": cap - completed - reserved}

    def create_request(self, text: str) -> dict[str, Any]:
        scope = self.scope()
        self.connection.execute("UPDATE scopes SET confirmed_at=? WHERE scope_id=?", (now(), scope["scope_id"]))
        request_id, created = str(uuid.uuid4()), now()
        self.connection.execute("""INSERT INTO purchase_requests(request_id, scope_id, scope_version, user_text, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'DRAFT', ?, ?)""", (request_id, scope["scope_id"], scope["version"], text, created, created))
        # User prose may contain identifying data; the public passport records only trusted scope.
        self.add_event(request_id, "USER_SCOPE", "scope_confirmed", {key: scope[key] for key in (
            "business_label", "market", "currency", "merchant_key", "approved_product_type", "permitted_quantity",
            "per_checkout_cap_minor", "total_budget_minor", "version")})
        return self.request(request_id)

    def request(self, request_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM purchase_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        result = dict(row)
        result["candidate"] = json.loads(result.pop("candidate_json")) if result.get("candidate_json") else None
        result["quote"] = json.loads(result.pop("quote_json")) if result.get("quote_json") else None
        attempt = self.connection.execute("SELECT * FROM checkout_attempts WHERE request_id=?", (request_id,)).fetchone()
        result["attempt"] = dict(attempt) if attempt else None
        result["has_pending_attempt"] = bool(attempt)
        return result

    def save_discovery(self, request_id: str, candidate: dict[str, Any], quote: dict[str, Any], mode: str) -> None:
        if self.request(request_id)["attempt"]:
            raise ValueError("a checkout attempt already exists for this request")
        total = quote.get("total_minor")
        self.connection.execute("""UPDATE purchase_requests SET product_id=?, variant_id=?, merchant_key=?, quantity=?, quote_id=?, quote_total_minor=?,
            quote_currency=?, quote_expires_at=?, candidate_json=?, quote_json=?, source_mode=?, state='QUOTED', updated_at=? WHERE request_id=?""",
            (candidate.get("product_id"), candidate.get("variant_id"), candidate.get("merchant_key"), self.scope()["permitted_quantity"],
             quote.get("quote_id"), total if type(total) is int else None, quote.get("currency"), quote.get("expires_at"),
             _json(candidate), _json(quote), mode, now(), request_id))

    def set_state(self, request_id: str, state: str) -> None:
        self.connection.execute("UPDATE purchase_requests SET state=?, updated_at=? WHERE request_id=?", (state, now(), request_id))

    def mark_review_required(self, request_id: str, detail: str) -> None:
        self.set_state(request_id, "REVIEW_REQUIRED")
        self.add_event(request_id, "APP_POLICY", "quote_changed", {"detail": detail})

    def mark_blocked(self, request_id: str, detail: str) -> None:
        self.set_state(request_id, "BLOCKED")
        self.add_event(request_id, "APP_POLICY", "blocked", {"detail": detail})

    def add_event(self, request_id: str, source: str, event_type: str, payload: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO events(request_id,source_label,event_type,safe_payload_json,created_at) VALUES (?, ?, ?, ?, ?)",
                                (request_id, source, event_type, _json(payload), now()))

    def events(self, request_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events WHERE request_id=? ORDER BY event_id", (request_id,)).fetchall()
        return [{"source": row["source_label"], "event_type": row["event_type"], "payload": json.loads(row["safe_payload_json"]), "created_at": row["created_at"]} for row in rows]

    def reserve_attempt(self, request_id: str, reviewed_request: dict[str, Any], amount: int, payload: dict[str, Any], state: str) -> tuple[dict[str, Any], bool]:
        """Commit the exact payload and reservation before any checkout network call."""
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                request = self.request(request_id)
                if request["attempt"]:
                    self.connection.execute("COMMIT")
                    return request["attempt"], False
                scope, budget = self.scope(), self.budget_snapshot()
                binding = ("scope_id", "scope_version", "product_id", "variant_id", "merchant_key", "quantity", "quote_id", "quote_total_minor", "quote_currency", "quote_expires_at", "source_mode")
                if any(request[key] != reviewed_request[key] for key in binding) or request["state"] != "QUOTED" or request["scope_version"] != scope["version"]:
                    raise ValueError("request changed before reservation; review again")
                if type(amount) is not int or amount <= 0 or amount > budget["available_minor"] or amount > scope["per_checkout_cap_minor"] or scope["suspended"]:
                    raise ValueError("scope or available budget changed before reservation")
                timestamp, body = now(), _json(payload)
                attempt = {"attempt_id": str(uuid.uuid4()), "request_id": request_id, "idempotency_key": str(uuid.uuid4()),
                           "quote_id": payload["quote_id"], "enrollment_id": payload["enrollment_id"], "request_body_hash": sha256(body.encode()).hexdigest(),
                           "request_body_json": body, "reserved_minor": amount, "checkout_id": None, "checkout_status": "RESERVED", "settlement_state": "HELD",
                           "order_id": None, "final_amount_minor": None, "next_action_url": None, "return_state_hash": sha256(state.encode()).hexdigest(),
                           "created_at": timestamp, "updated_at": timestamp}
                self.connection.execute("INSERT INTO checkout_attempts VALUES (" + ",".join(":" + key for key in attempt) + ")", attempt)
                self.set_state(request_id, "RESERVED")
                self.add_event(request_id, "APP_POLICY", "budget_reserved", {"reserved_minor": amount})
                self.connection.execute("COMMIT")
                return attempt, True
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def update_attempt(self, request_id: str, checkout: dict[str, Any], *, unknown: bool = False) -> dict[str, Any]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                attempt = self.request(request_id)["attempt"]
                if attempt is None:
                    raise KeyError(request_id)
                if attempt["settlement_state"] != "HELD":
                    self.connection.execute("COMMIT")
                    return attempt
                status = "UNKNOWN" if unknown else checkout.get("status", "UNKNOWN")
                if status not in {"REQUIRES_ACTION", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED", "UNKNOWN"}:
                    status = "UNKNOWN"
                checkout_id = checkout.get("checkout_id")
                valid_id = isinstance(checkout_id, str) and bool(checkout_id)
                identity_changed = bool(attempt["checkout_id"] and checkout_id != attempt["checkout_id"])
                if not valid_id or identity_changed:
                    status = "UNKNOWN"
                    checkout_id = attempt["checkout_id"]
                if identity_changed:
                    self.connection.execute("UPDATE scopes SET suspended=1")
                    self.add_event(request_id, "APP_POLICY", "checkout_identity_discrepancy", {"detail": "status response did not match the stored checkout"})
                final = checkout.get("final_amount_minor")
                settlement = "HELD"
                if status == "COMPLETED":
                    if type(final) is not int or final <= 0 or checkout.get("currency") != "SGD":
                        self.connection.execute("UPDATE scopes SET suspended=1")
                        self.add_event(request_id, "APP_POLICY", "settlement_unverified", {"detail": "COMPLETED reported; actual SGD amount is unverified, reservation held"})
                        final = None
                    else:
                        settlement = "SETTLED"
                        scope, budget = self.scope(), self.budget_snapshot()
                        if final != attempt["reserved_minor"] or final > scope["per_checkout_cap_minor"] or final > budget["available_minor"] + attempt["reserved_minor"]:
                            settlement = "DISCREPANCY"
                            self.connection.execute("UPDATE scopes SET suspended=1")
                            self.add_event(request_id, "APP_POLICY", "settlement_discrepancy", {"reserved_minor": attempt["reserved_minor"], "final_amount_minor": final})
                elif status in {"FAILED", "EXPIRED"}:
                    settlement, final = "RELEASED", None
                else:
                    final = None
                if attempt["checkout_status"] == "COMPLETED" and status != "COMPLETED":
                    status, settlement = "COMPLETED", "HELD"
                hosted = checkout.get("next_action_url") if status == "REQUIRES_ACTION" and valid_id and not identity_changed else None
                order = checkout.get("order_id") if valid_id and not identity_changed else None
                self.connection.execute("""UPDATE checkout_attempts SET checkout_id=?, checkout_status=?, settlement_state=?, order_id=COALESCE(?,order_id),
                    final_amount_minor=COALESCE(?,final_amount_minor), next_action_url=?, updated_at=? WHERE request_id=?""",
                    (checkout_id, status, settlement, order, final, hosted, now(), request_id))
                self.set_state(request_id, status)
                self.connection.execute("COMMIT")
                return self.request(request_id)["attempt"]
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def attempt_for_state(self, state: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM checkout_attempts WHERE return_state_hash=?", (sha256(state.encode()).hexdigest(),)).fetchone()
        return dict(row) if row else None
