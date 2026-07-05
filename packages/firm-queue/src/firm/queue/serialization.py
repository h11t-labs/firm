"""Job-argument serialization.

Arguments are stored as compact JSON in ``jobs.arguments``. Plain JSON types pass through;
a few common non-JSON types (datetime/date/Decimal/UUID) round-trip via the shared
tagged-object protocol (:mod:`firm._core.tagged_json`, reserved key ``__firm_queue__``).
Anything else — including a dict using the reserved key — raises **at enqueue time** so the
failure is the caller's, not a worker's hours later.
"""

from __future__ import annotations

from typing import Any

from .._core.tagged_json import TaggedJSON

_codec = TaggedJSON("__firm_queue__", value_noun="a serializable job argument")


def serialize(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return _codec.dumps({"args": list(args), "kwargs": kwargs})


def deserialize(blob: str | None) -> tuple[list[Any], dict[str, Any]]:
    if not blob:
        return [], {}
    data = _codec.loads(blob)
    return data.get("args", []), data.get("kwargs", {})
