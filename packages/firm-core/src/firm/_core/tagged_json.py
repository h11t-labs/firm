"""Tagged-JSON codec shared by firm.queue (job arguments) and firm.audit (event payloads).

Plain JSON types pass through; a few common non-JSON types (datetime/date/Decimal/UUID)
round-trip via a tagged-object envelope ``{<tag>: "<type>", "v": "<string>"}``. Each consumer
picks its own reserved tag key so the two wire formats stay independent.

The codec is strict in both directions: encoding rejects user dicts that contain the reserved
key (so data that *looks* like the envelope fails fast at write time, in the caller's stack),
and decoding only treats a dict as tagged when it has exactly the envelope shape with a known
type — anything else passes through as a plain dict.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


class TaggedJSON:
    """A compact-JSON codec with a per-consumer reserved tag key.

    ``value_noun`` names what is being serialized in error messages (e.g. "a serializable
    job argument").
    """

    def __init__(self, tag: str, *, value_noun: str) -> None:
        self.tag = tag
        self.value_noun = value_noun

    def dumps(self, obj: Any) -> str:
        self._reject_reserved_key(obj)
        return json.dumps(obj, default=self._encode_default, separators=(",", ":"))

    def loads(self, blob: str) -> Any:
        return json.loads(blob, object_hook=self._object_hook)

    def _reject_reserved_key(self, obj: Any) -> None:
        if isinstance(obj, dict):
            if self.tag in obj:
                raise ValueError(
                    f"the dict key {self.tag!r} is reserved by firm's serialization envelope"
                )
            for value in obj.values():
                self._reject_reserved_key(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                self._reject_reserved_key(value)

    def _encode_default(self, obj: Any) -> dict[str, str]:
        if isinstance(obj, datetime):
            return {self.tag: "datetime", "v": obj.isoformat()}
        if isinstance(obj, date):
            return {self.tag: "date", "v": obj.isoformat()}
        if isinstance(obj, Decimal):
            return {self.tag: "decimal", "v": str(obj)}
        if isinstance(obj, UUID):
            return {self.tag: "uuid", "v": str(obj)}
        raise TypeError(
            f"{type(obj).__name__} is not {self.value_noun}; pass JSON-native values "
            "(or datetime/date/Decimal/UUID)."
        )

    def _object_hook(self, d: dict[str, Any]) -> Any:
        tag = d.get(self.tag)
        # Only the exact envelope shape decodes: {tag: <known type>, "v": <string>}.
        if tag is None or len(d) != 2 or not isinstance(d.get("v"), str):
            return d
        value = d["v"]
        if tag == "datetime":
            return datetime.fromisoformat(value)
        if tag == "date":
            return date.fromisoformat(value)
        if tag == "decimal":
            return Decimal(value)
        if tag == "uuid":
            return UUID(value)
        return d
