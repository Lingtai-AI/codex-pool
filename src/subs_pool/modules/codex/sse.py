"""Minimal SSE encode/decode.

The decoder is chunk-boundary agnostic: it is fed arbitrary byte chunks (as
real network reads arrive) and only ever yields a complete event once a blank
line terminator has actually been seen, regardless of where chunk boundaries
fell across ``data:``/``event:`` lines or even mid-line.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator
from typing import Any


def encode_event(event_type: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


class SSEDecoder:
    """Feed raw bytes in any chunking; get back complete parsed events.

    Decoding is incremental (``codecs.IncrementalDecoder``) so a multibyte
    UTF-8 character split across two ``feed()`` calls decodes correctly
    instead of raising on the first, truncated half. Line splitting
    recognizes ``\\n``, ``\\r\\n``, and bare ``\\r`` terminators. A trailing
    carriage return is held until the next chunk disambiguates CRLF from a
    bare CR; :meth:`finish` resolves it as a bare terminator at EOF.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._data_lines: list[str] = []
        self._event_name: str | None = None
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

    def feed(self, chunk: bytes | str) -> list[dict[str, Any]]:
        if isinstance(chunk, (bytes, bytearray)):
            self._buffer += self._utf8_decoder.decode(bytes(chunk))
        else:
            self._buffer += chunk
        return self._drain(final=False)

    def finish(self) -> list[dict[str, Any]]:
        """Finish a stream, raising on an incomplete UTF-8 sequence.

        SSE dispatch requires a blank-line terminator, so a final unterminated
        data line is intentionally discarded. A final bare CR is nevertheless
        a valid line terminator and is processed before that discard.
        """
        self._buffer += self._utf8_decoder.decode(b"", final=True)
        events = self._drain(final=True)
        # A provider that closes with a data line but no blank-line dispatch
        # has not delivered a complete SSE event. Do not silently turn that
        # partial response into success; comments may be left unterminated and
        # are harmlessly ignored.
        if self._data_lines:
            raise ValueError("incomplete SSE event at end of stream")
        pending = self._buffer.strip()
        if pending.startswith(("data:", "event:")):
            raise ValueError("incomplete SSE line at end of stream")
        return events

    def _drain(self, *, final: bool) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            line = self._pop_line(final=final)
            if line is None:
                break
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        return events

    def _pop_line(self, *, final: bool) -> str | None:
        nl_idx = self._buffer.find("\n")
        cr_idx = self._buffer.find("\r")
        if cr_idx != -1 and (nl_idx == -1 or cr_idx < nl_idx):
            if cr_idx == len(self._buffer) - 1 and not final:
                return None  # could be a lone CR or the start of CRLF
            end = cr_idx + 2 if cr_idx + 1 < len(self._buffer) and self._buffer[cr_idx + 1] == "\n" else cr_idx + 1
            line, self._buffer = self._buffer[:cr_idx], self._buffer[end:]
            return line
        if nl_idx != -1:
            line, self._buffer = self._buffer[:nl_idx], self._buffer[nl_idx + 1 :]
            return line
        return None

    def _consume_line(self, line: str) -> dict[str, Any] | None:
        if line == "":
            return self._flush()
        if line.startswith(":"):
            return None
        if line.startswith("event:"):
            self._event_name = line[6:].strip() or None
            return None
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip())
            return None
        return None

    def _flush(self) -> dict[str, Any] | None:
        data_lines, self._data_lines = self._data_lines, []
        event_name, self._event_name = self._event_name, None
        if not data_lines:
            return None
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            return None
        decoded = json.loads(payload)
        if isinstance(decoded, dict) and not decoded.get("type") and event_name:
            decoded["type"] = event_name
        return decoded if isinstance(decoded, dict) else {"type": event_name, "data": decoded}


async def decode_sse_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    decoder = SSEDecoder()
    async for chunk in chunks:
        for event in decoder.feed(chunk):
            yield event
    for event in decoder.finish():
        yield event
