"""Local error types mapped to OpenAI-shaped error JSON bodies."""

from __future__ import annotations


class PoolRequestError(Exception):
    """A client request this proxy rejects before touching upstream."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def to_body(self) -> dict:
        return {"error": {"message": self.message, "type": self.code, "code": self.code}}


class UpstreamTransportError(Exception):
    """A genuine transport-level failure talking to the upstream Codex backend."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_body(self) -> dict:
        return {"error": {"message": self.message, "type": "upstream_error", "code": "upstream_error"}}
