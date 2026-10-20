"""Storage-independent, signed event envelopes for durable retries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .event_format import canonical
from .integrity import canonical_created_at
from .object_store import UnknownWriteOutcome

EVENT_FIELDS = (
    "action",
    "subject_type",
    "subject_id",
    "subject_label",
    "actor_type",
    "actor_id",
    "actor_label",
    "correlation_id",
    "data",
    "changes",
    "context",
)


@dataclass(frozen=True)
class PreparedEvent:
    """One event, fully formed (entry id, timestamp, signature) but not yet written.

    Persist ``body`` to retry identically after a process restart: every retry of the same body
    lands on the same event, whichever process sends it.
    """

    body: str

    @property
    def entry_id(self) -> str:
        return str(json.loads(self.body)["entry_id"])


class UnknownEventOutcome(UnknownWriteOutcome):
    """The prepared event must be retried; its previous outcome is unknown."""

    def __init__(self, prepared: PreparedEvent) -> None:
        super().__init__("Audit write outcome unknown; retry exception.prepared")
        self.prepared = prepared


def freeze_values(values: dict[str, Any]) -> PreparedEvent:
    """Freeze the exact stored JSON strings and timestamp, including signature metadata."""
    body: dict[str, Any] = {**dict.fromkeys((*EVENT_FIELDS, "row_mac", "key_id")), **values}
    body["created_at"] = canonical_created_at(body["created_at"])
    return PreparedEvent(canonical(body).decode())


def decode_prepared(prepared: PreparedEvent) -> dict[str, Any]:
    value = json.loads(prepared.body)
    if not isinstance(value, dict) or canonical(value).decode() != prepared.body:
        raise ValueError("event body must retain the canonical format produced by prepare()")
    allowed = {*EVENT_FIELDS, "entry_id", "created_at", "row_mac", "key_id", "provenance"}
    if set(value) - allowed or not isinstance(value.get("action"), str):
        raise ValueError("not a prepared audit event")
    if not isinstance(value.get("entry_id"), str) or not value["entry_id"]:
        raise ValueError("prepared event has no entry id")
    datetime.fromisoformat(value["created_at"])
    return value
