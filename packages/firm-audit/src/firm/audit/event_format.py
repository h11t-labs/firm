"""Versioned, lossless audit envelopes and validated scan continuation tokens."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .object_store import digest
from .serialization import dump_json, load_json

FIELDS = (
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
    "created_at",
)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def utc(value: datetime) -> datetime:
    # SQL timestamps are deliberately stored as naive UTC by firm-core.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def uuid_text(value: str) -> str:
    return str(UUID(value))


def wire_event(row: dict[str, Any]) -> dict[str, Any]:
    result = {field: row[field] for field in FIELDS}
    result["created_at"] = utc(row["created_at"]).isoformat()
    for field in ("data", "changes", "context"):
        result[field] = dump_json(row[field])
    return result


def read_event(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != set(FIELDS) or not isinstance(value["action"], str):
        raise ValueError("invalid audit event fields")
    result = dict(value)
    result["created_at"] = utc(datetime.fromisoformat(value["created_at"]))
    for field in ("data", "changes", "context"):
        result[field] = load_json(value[field])
    return result


def token_encode(query: Any, position: Any) -> str:
    return base64.urlsafe_b64encode(
        canonical({"v": 1, "q": digest(canonical(query)), "p": position})
    ).decode()


def token_decode(token: str, query: Any) -> Any:
    if len(token) > 8192:
        raise ValueError("invalid continuation")
    try:
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if value["v"] != 1 or value["q"] != digest(canonical(query)):
            raise ValueError("continuation belongs to a different query")
        return value["p"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid continuation") from exc
