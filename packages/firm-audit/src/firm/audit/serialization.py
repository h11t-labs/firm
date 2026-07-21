"""JSON codec for the audit log's ``data``/``changes``/``context`` columns.

``load_json`` is a supported read surface — the dashboard (firm-ui) decodes stored payloads
with it, like it reads ``schema.audit_events``. Changing its signature is a breaking change.

Stored as plain ``Text`` (matching :mod:`firm.queue`'s argument serialization, not native JSON/
JSONB) so the three columns are dialect-uniform but not SQL-filterable —
:func:`~firm.audit.log.AuditLog.history` only ever filters on the indexed scalar columns.

Non-JSON types (datetime/date/Decimal/UUID) round-trip via the shared tagged-object protocol
(:mod:`firm._core.tagged_json`), with ``__firm_audit__`` as this module's reserved key so the
wire format stays independent of the queue's.
"""

from __future__ import annotations

from typing import Any

from .._core.tagged_json import TaggedJSON

_codec = TaggedJSON("__firm_audit__", value_noun="JSON-serializable in an audit payload")


def dump_json(obj: dict[str, Any] | None) -> str | None:
    if obj is None:
        return None
    return _codec.dumps(obj)


def load_json(blob: str | None) -> dict[str, Any] | None:
    if blob is None:
        return None
    return _codec.loads(blob)
