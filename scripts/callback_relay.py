"""Keep Reap's public HTTPS callbacks outside the loopback-only demo app."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit, urlunsplit


STATE = re.compile(r"[A-Za-z0-9_-]{43}")


def _local_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("local origin must be a loopback HTTP origin") from exc
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
        or urlunsplit(parts) != value
    ):
        raise ValueError("local origin must be a loopback HTTP origin")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("local origin must use a valid port") from exc
    return value


def _public_host(value: str) -> str:
    try:
        parts = urlsplit(f"//{value}")
    except ValueError as exc:
        raise ValueError("public host must be a hostname without a scheme or path") from exc
    if not value or parts.hostname is None or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError("public host must be a hostname without a scheme or path")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("public host must use a valid port") from exc
    return value


@dataclass(frozen=True)
class RelayConfig:
    public_host: str
    local_origin: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_host", _public_host(self.public_host))
        object.__setattr__(self, "local_origin", _local_origin(self.local_origin))


class CallbackRelay(BaseHTTPRequestHandler):
    relay_config: RelayConfig

    def log_message(self, format: str, *args: object) -> None:
        # Callback URLs contain opaque state and must never enter access logs.
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _reject(self, status: int, *, allow: str | None = None) -> None:
        self.send_response(status)
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get_all("Host") != [self.relay_config.public_host]:
            self._reject(403)
            return
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._reject(400)
            return
        if parts.scheme or parts.netloc:
            self._reject(400)
            return
        if parts.path == "/enrollment/return":
            self._redirect(f"{self.relay_config.local_origin}/?enrollment_return=1")
            return
        if parts.path != "/payment/return":
            self._reject(404)
            return
        try:
            fields = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            self._reject(400)
            return
        states = [value for key, value in fields if key == "state"]
        if len(states) != 1 or not STATE.fullmatch(states[0]):
            self._reject(400)
            return
        self._redirect(f"{self.relay_config.local_origin}/payment/return?state={states[0]}")

    def do_POST(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject(405, allow="GET")

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


def relay_handler(config: RelayConfig) -> type[CallbackRelay]:
    class ConfiguredCallbackRelay(CallbackRelay):
        relay_config = config

    return ConfiguredCallbackRelay


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay one configured public Reap callback host to the local demo.")
    parser.add_argument("--public-host", required=True, help="Exact Host header accepted from the HTTPS tunnel")
    parser.add_argument("--local-origin", default=os.getenv("SPENDPILOT_ORIGIN", "http://127.0.0.1:8000"))
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    config = RelayConfig(args.public_host, args.local_origin)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), relay_handler(config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
