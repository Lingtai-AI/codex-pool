"""Canonical, order-preserving content hashing for full-prefix affinity.

Used only as a routing index — never as an authorization identity and never
sent upstream. Hashing preserves all item fields except one proven-equivalent
plain assistant output/input representation documented in ``_canonical_item``.
Cost is O(n) per request (one rolling pass over the candidate ``input`` array)
plus O(records) integer comparisons; it never rehashes a stored baseline's own
bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic serialization of an already-selected hash value."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _canonical_item(item: Any) -> Any:
    """Return the only proven output/input-equivalent item representation.

    The OpenAI Responses API represents a plain assistant message returned by
    the model as ``type=message`` with one ``output_text`` content part, while
    a subsequent full-history request can replay it as ``role=assistant`` with
    a string ``content``. Generated ``id`` and completed ``status`` fields are
    known output-envelope metadata and do not change that text. Every other
    field, shape, role, annotation, or content type remains byte-for-byte
    represented by the ordinary JSON serialization below.
    """
    if not isinstance(item, dict):
        return item

    # Do not discard unknown fields from input messages. In particular, an
    # assistant item with any extra field must not collapse with an output
    # envelope merely because its text happens to match.
    if set(item) == {"role", "content"} and item.get("role") == "assistant" and isinstance(item["content"], str):
        return {"role": "assistant", "content": item["content"]}

    if item.get("type") != "message" or item.get("role") != "assistant":
        return item
    if not {"type", "role", "content"}.issubset(item):
        return item
    if not set(item).issubset({"type", "id", "role", "status", "content"}):
        return item
    if "id" in item and (not isinstance(item["id"], str) or not item["id"]):
        return item
    if "status" in item and item["status"] != "completed":
        return item

    content = item["content"]
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return item
    part = content[0]
    if not {"type", "text"}.issubset(part) or not set(part).issubset({"type", "text", "annotations"}):
        return item
    if part.get("type") != "output_text" or not isinstance(part["text"], str):
        return item
    if "annotations" in part and part["annotations"] != []:
        return item
    return {"role": "assistant", "content": part["text"]}


def item_hash(item: Any) -> str:
    return hashlib.sha256(canonical_json(_canonical_item(item)).encode("utf-8")).hexdigest()


def rolling_hashes(items: list[Any], start: str = _EMPTY_HASH) -> list[str]:
    """Return ``H`` where ``H[0] == start`` and ``H[i]`` folds in ``items[i-1]``.

    ``H[i]`` is the affinity hash of the first ``i`` items. Comparing a stored
    baseline's ``(length, hash)`` against ``H[length]`` answers "is this
    baseline a full prefix of the candidate content?" in O(1) once ``H`` is
    built, so checking many stored chains against one incoming request stays
    linear in the request size, not quadratic in history.
    """
    out = [start]
    h = start
    for item in items:
        h = hashlib.sha256((h + item_hash(item)).encode("utf-8")).hexdigest()
        out.append(h)
    return out


def extend_hash(start: str, items: list[Any]) -> str:
    """Fold ``items`` onto an existing rolling hash without recomputing it."""
    return rolling_hashes(items, start=start)[-1]


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()
