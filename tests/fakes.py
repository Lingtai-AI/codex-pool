"""Fake upstream + auth fixture helpers used only by tests.

The fake upstream speaks the same event vocabulary a real Codex/Responses
backend would emit over SSE, but is driven entirely by an in-test script — no
network, no real endpoint URL, dependency-injected into ``create_app``.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from subs_pool.modules.codex.errors import UpstreamTransportError


def write_auth_fixture(
    path: Path,
    *,
    refresh_token: str = "rt-1",
    expires_in: int = 3600,
    account_id: str = "acct-1",
) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "at-1",
                "refresh_token": refresh_token,
                "expires_at": int(time.time()) + expires_in,
                "account_id": account_id,
            }
        ),
        encoding="utf-8",
    )


def _response_object(
    *,
    response_id: str,
    model: str,
    output: list[dict],
    status: str = "completed",
    usage: dict | None = None,
    error: dict | None = None,
) -> dict:
    obj = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": status,
        "output": output,
        "output_text": "".join(
            part.get("text", "")
            for item in output
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ),
        "usage": usage or {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
    }
    if error is not None:
        obj["error"] = error
    return obj


class ScriptedUpstream:
    """Replay fixed scripted turns without network or provider I/O."""

    def __init__(self) -> None:
        self.turns: list[Any] = []
        self.calls: list[dict] = []

    def queue_success(self, *, model: str, output: list[dict], response_id: str | None = None) -> None:
        self.turns.append(("success", model, output, response_id))

    def queue_partial_then_fail(self, *, model: str, partial_item: dict) -> None:
        self.turns.append(("partial_fail", model, partial_item))

    def queue_transport_error(self) -> None:
        self.turns.append(("transport_error",))

    def queue_success_output_via_done_events_only(
        self, *, model: str, output: list[dict], response_id: str | None = None
    ) -> None:
        """Emit real output only as ``response.output_item.done`` events."""
        self.turns.append(("success_done_only", model, output, response_id))

    def queue_completed_with_unobservable_output(
        self, *, model: str, response_id: str | None = None
    ) -> None:
        """Emit completed status with no output field or done items."""
        self.turns.append(("completed_unobservable", model, response_id))

    async def stream(
        self,
        *,
        access_token: str,
        account_id: str | None,
        payload: dict,
        session_id: str | None = None,
        thread_id: str | None = None,
    ) -> AsyncIterator[dict]:
        self.calls.append(
            {
                "access_token": access_token,
                "account_id": account_id,
                "payload": payload,
                "session_id": session_id,
                "thread_id": thread_id,
            }
        )
        if not self.turns:
            raise AssertionError("ScriptedUpstream: no more turns queued")
        turn = self.turns.pop(0)
        kind = turn[0]

        if kind == "transport_error":
            raise UpstreamTransportError("simulated connection reset")

        response_id = "resp_" + str(len(self.calls))
        if kind == "partial_fail":
            _, _model, partial_item = turn
            yield {"type": "response.output_item.added", "output_index": 0, "item": partial_item}
            raise UpstreamTransportError("simulated mid-stream drop")

        if kind == "success_done_only":
            _, model, output, forced_id = turn
            response_id = forced_id or response_id
            yield {"type": "response.created", "response": _response_object(response_id=response_id, model=model, output=[], status="in_progress")}
            for idx, item in enumerate(output):
                yield {"type": "response.output_item.added", "output_index": idx, "item": item}
                yield {"type": "response.output_item.done", "output_index": idx, "item": item}
            final = _response_object(response_id=response_id, model=model, output=output)
            del final["output"]
            yield {"type": "response.completed", "response": final}
            return

        if kind == "completed_unobservable":
            _, model, forced_id = turn
            response_id = forced_id or response_id
            yield {"type": "response.created", "response": _response_object(response_id=response_id, model=model, output=[], status="in_progress")}
            yield {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": model,
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                },
            }
            return

        _, model, output, forced_id = turn
        response_id = forced_id or response_id
        yield {"type": "response.created", "response": _response_object(response_id=response_id, model=model, output=[], status="in_progress")}
        for idx, item in enumerate(output):
            yield {"type": "response.output_item.added", "output_index": idx, "item": item}
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    text = part.get("text", "")
                    for i in range(0, len(text), 3):
                        yield {
                            "type": "response.output_text.delta",
                            "output_index": idx,
                            "delta": text[i : i + 3],
                        }
            yield {"type": "response.output_item.done", "output_index": idx, "item": item}
        final = _response_object(response_id=response_id, model=model, output=output)
        yield {"type": "response.completed", "response": final}
