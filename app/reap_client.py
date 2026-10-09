"""Small, safe async transport for the documented Reap Agentic API."""

from __future__ import annotations

import os
from typing import Any

import httpx


class ReapError(Exception):
    """Base error for the Reap integration."""


class ReapConfigurationError(ReapError):
    """Raised when required non-secret configuration is absent."""


class ReapMalformedResponse(ReapError):
    """Raised when a successful Reap response cannot be normalized safely."""


class ReapTransportError(ReapError):
    """Raised for network errors where no payment state is implied."""


class ReapCheckoutOutcomeUnknown(ReapTransportError):
    """Checkout POST may have reached Reap; keep the caller's reservation."""


class ReapAPIError(ReapError):
    """A safe representation of Reap's structured API error."""

    def __init__(self, code: str, status: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        # Details can contain validation echoes, so they intentionally stay out of logs.
        return f"Reap API error {self.status} ({self.code}): {self.message}"


class ReapClient:
    """HTTP transport with the mandatory Reap authentication/version headers."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        version: str | None = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("REAP_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("REAP_API_KEY") or ""
        self.version = version or os.getenv("REAP_VERSION") or ""
        if not self.base_url:
            raise ReapConfigurationError("REAP_BASE_URL is required")
        if not self.api_key:
            raise ReapConfigurationError("REAP_API_KEY is required")
        if not self.version:
            raise ReapConfigurationError("REAP_VERSION is required")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ReapClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Reap-Version": self.version,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = await self._client.request(method, path, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            if method.upper() == "POST" and path == "/agentic/checkouts":
                raise ReapCheckoutOutcomeUnknown(
                    "Checkout request timed out; its outcome is unknown. Do not create another checkout."
                ) from exc
            raise ReapTransportError("Reap request timed out") from exc
        except httpx.HTTPError as exc:
            if method.upper() == "POST" and path == "/agentic/checkouts":
                raise ReapCheckoutOutcomeUnknown(
                    "Checkout request could not be confirmed; its outcome is unknown. Do not create another checkout."
                ) from exc
            raise ReapTransportError("Could not reach Reap") from exc

        parsed: Any
        try:
            parsed = response.json()
        except ValueError:
            parsed = None

        if response.is_error:
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            raise ReapAPIError(
                code=str(error.get("code") or "HTTP_ERROR"),
                status=response.status_code,
                message=str(error.get("message") or "Reap returned an error"),
                detail=error.get("detail"),
            )
        if not isinstance(parsed, dict):
            raise ReapMalformedResponse("Reap returned a non-object JSON response")
        return parsed
