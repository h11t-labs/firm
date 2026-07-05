"""Argument serialization specs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from firm.queue.serialization import deserialize, serialize


def test_roundtrip_json_native() -> None:
    blob = serialize((1, "a", [1, 2], {"k": True}), {"x": None})
    args, kwargs = deserialize(blob)
    assert args == [1, "a", [1, 2], {"k": True}]
    assert kwargs == {"x": None}


def test_roundtrip_rich_types() -> None:
    values = (
        datetime(2026, 6, 28, 12, 30),
        date(2026, 6, 28),
        Decimal("3.14"),
        UUID("12345678-1234-5678-1234-567812345678"),
    )
    args, _ = deserialize(serialize(values, {}))
    assert tuple(args) == values


def test_empty_blob() -> None:
    assert deserialize(None) == ([], {})
    assert deserialize("") == ([], {})


def test_non_serializable_raises_at_enqueue_time() -> None:
    with pytest.raises(TypeError):
        serialize((object(),), {})


def test_reserved_key_rejected_at_enqueue_time() -> None:
    """A user dict using the envelope's reserved key fails at serialize time (the caller's
    stack), instead of silently round-tripping into a different type on the worker."""
    with pytest.raises(ValueError, match="reserved"):
        serialize(({"__firm_queue__": "datetime", "v": "2026-01-01T00:00:00"},), {})
    with pytest.raises(ValueError, match="reserved"):
        serialize((), {"payload": {"nested": [{"__firm_queue__": "x"}]}})


def test_decode_is_shape_strict() -> None:
    """Only the exact envelope shape decodes; look-alike dicts written by other producers
    come back as plain dicts instead of corrupting or raising KeyError on the worker."""
    args, _ = deserialize('{"args":[{"__firm_queue__":"datetime"}],"kwargs":{}}')
    assert args == [{"__firm_queue__": "datetime"}]  # no "v": not an envelope
    args, _ = deserialize('{"args":[{"__firm_queue__":"nope","v":"x"}],"kwargs":{}}')
    assert args == [{"__firm_queue__": "nope", "v": "x"}]  # unknown tag: left alone
    args, _ = deserialize('{"args":[{"__firm_queue__":"uuid","v":"y","extra":1}],"kwargs":{}}')
    assert args == [{"__firm_queue__": "uuid", "v": "y", "extra": 1}]  # extra key: left alone
