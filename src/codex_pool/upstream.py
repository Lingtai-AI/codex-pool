"""Upstream Port: how the local Responses server talks to a real Codex backend.

``Upstream`` is a small ``Protocol`` so tests inject a fake implementation
(see tests/fakes.py) instead of a caller-supplied arbitrary URL. The actual
production adapter (``CodexHTTPUpstream``) is the only piece that may open a
live connection, and its base URL is fixed at construction time, never taken
from a request body or header.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from .errors import UpstreamTransportError
from .sse import decode_sse_stream

# Real Codex backend base (see reference/lingtai-kernel
# src/lingtai/llm/_register.py CODEX_OFFICIAL_BASE_URL).
CODEX_OFFICIAL_BASE_URL = "https://chatgpt.com/backend-api/codex"


class Upstream(Protocol):
    async def stream(
        self, *, access_token: str, account_id: str | None, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield already-decoded Responses-API SSE event dicts, in order."""
        ...


# Provider-controlled strings are not safe to echo merely because they appear
# in a JSON field named ``type`` or ``code``. Keep the useful, documented
# machine vocabulary bounded; all other detail is deliberately omitted.
_SAFE_PROVIDER_CODES = frozenset(
    {
        "invalid_auth",
        "invalid_api_key",
        "token_expired",
        "usage_limit_reached",
        "rate_limit_exceeded",
        "insufficient_quota",
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "rate_limit_error",
        "api_error",
        "server_error",
        "overloaded_error",
        "bad_request",
    }
)


def _redacted_error_detail(raw: bytes) -> str | None:
    """Extract only bounded, known-safe ``type``/``code`` values.

    The raw bytes may echo request content (including, in principle, the
    bearer token or account data); the raw body and arbitrary provider strings
    are never surfaced.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(err, dict):
        return None
    values = []
    for key in ("type", "code"):
        value = err.get(key)
        if isinstance(value, str) and value in _SAFE_PROVIDER_CODES:
            values.append(value)
    return "/".join(dict.fromkeys(values)) or None


class CodexHTTPUpstream:
    """Real HTTP adapter.

    ``transport`` is an optional injected ``httpx.AsyncBaseTransport`` (for
    example, ``httpx.MockTransport``) used by tests without opening a socket;
    production code leaves it unset and gets httpx's normal transport.
    """

    def __init__(
        self,
        base_url: str = CODEX_OFFICIAL_BASE_URL,
        *,
        timeout: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def stream(
        self, *, access_token: str, account_id: str | None, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id

        body = dict(payload)
        body["stream"] = True

        client_kwargs: dict[str, Any] = {"timeout": self._timeout, "trust_env": False}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "POST", f"{self._base_url}/responses", headers=headers, json=body
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raw = await response.aread()
                        detail = _redacted_error_detail(raw)
                        message = f"Codex upstream returned HTTP {response.status_code}"
                        if detail:
                            message += f" ({detail})"
                        raise UpstreamTransportError(message, status_code=502)
                    async for event in decode_sse_stream(response.aiter_bytes()):
                        yield event
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            # Malformed SSE/JSON is a failed upstream turn, never a successful
            # empty completion; expose only a bounded typed reason.
            raise UpstreamTransportError("Codex upstream returned malformed event data", status_code=502) from exc
        except httpx.TransportError as exc:
            # Deliberately not ``str(exc)``: some httpx transport errors can
            # embed request context. Only the exception class is surfaced.
            raise UpstreamTransportError(
                f"Codex upstream transport error: {type(exc).__name__}", status_code=502
            ) from exc
