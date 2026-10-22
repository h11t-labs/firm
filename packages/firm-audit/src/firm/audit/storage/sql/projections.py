"""Bounded SQL reads that tolerate attacker-controlled column values during verification."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, String, case, cast, func, or_, select

from ... import schema

if TYPE_CHECKING:
    from ...verify import SealRecords

_audits = schema.audit_events
_seals = schema.seals


def seal_records(conn: Connection) -> SealRecords:
    """Load side-table rows without invoking dialect datetime/integer result processors.

    Casting attacker-controlled typed fields to text lets verification classify malformed values
    instead of raising while SQLAlchemy decodes them.
    """

    from ... import verify as policy

    def bounded(column: Any, chars: int, label: str) -> Any:
        return func.substr(cast(column, String), 1, chars + 1).label(label)

    columns = (
        _seals.c.id,
        bounded(_seals.c.kind, 32, "kind"),
        bounded(_seals.c.from_id, 64, "from_id"),
        bounded(_seals.c.to_id, 64, "to_id"),
        bounded(_seals.c.row_count, 64, "row_count"),
        bounded(_seals.c.rows_mac, 64, "rows_mac"),
        bounded(_seals.c.seal_mac, 64, "seal_mac"),
        bounded(_seals.c.sealed_at, 64, "sealed_at"),
        bounded(_seals.c.key_id, 16, "key_id"),
    )
    records = policy.SealRecords()
    last_id = 0
    while len(records) < policy._MAX_SEAL_RECORDS:
        rows = conn.execute(
            select(*columns).where(_seals.c.id > last_id).order_by(_seals.c.id).limit(policy._PAGE)
        ).all()
        if not rows:
            break
        for row in rows:
            if len(records) >= policy._MAX_SEAL_RECORDS:
                records.capped = True
                break
            records.append(policy._parse_seal_record(row))
        last_id = rows[-1].id
        if len(rows) < policy._PAGE:
            break
    if len(records) == policy._MAX_SEAL_RECORDS:
        records.capped = (
            conn.execute(select(_seals.c.id).where(_seals.c.id > last_id).limit(1)).first()
            is not None
        )
    return records


def iter_rows(conn: Connection, low: int, high: int | None) -> Iterator[Any]:
    """Yield bounded event projections in id order with keyset pagination."""
    from ... import verify as policy

    text_limits = {
        "action": policy._MAX_SCALAR_CHARS,
        "subject_type": policy._MAX_SCALAR_CHARS,
        "subject_id": policy._MAX_SCALAR_CHARS,
        "subject_label": policy._MAX_SCALAR_CHARS,
        "actor_type": policy._MAX_SCALAR_CHARS,
        "actor_id": policy._MAX_SCALAR_CHARS,
        "actor_label": policy._MAX_SCALAR_CHARS,
        "correlation_id": policy._MAX_SCALAR_CHARS,
        "data": policy._MAX_JSON_CHARS,
        "changes": policy._MAX_JSON_CHARS,
        "context": policy._MAX_JSON_CHARS,
        "entry_id": 26,
        "row_mac": 64,
        "key_id": 16,
    }
    oversized = or_(
        *(func.length(_audits.c[name]) > limit for name, limit in text_limits.items()),
        func.length(cast(_audits.c.created_at, String)) > 64,
    )
    columns = [
        _audits.c.id,
        *(
            func.substr(_audits.c[name], 1, limit + 1).label(name)
            for name, limit in text_limits.items()
            if name not in {"entry_id", "row_mac", "key_id"}
        ),
        func.substr(cast(_audits.c.created_at, String), 1, 65).label("created_at"),
        func.substr(_audits.c.entry_id, 1, 27).label("entry_id"),
        func.substr(_audits.c.row_mac, 1, 65).label("row_mac"),
        func.substr(_audits.c.key_id, 1, 17).label("key_id"),
        case((oversized, True), else_=False).label("oversized"),
    ]
    last = low
    while True:
        stmt = select(*columns).where(_audits.c.id > last)
        if high is not None:
            stmt = stmt.where(_audits.c.id <= high)
        rows = conn.execute(stmt.order_by(_audits.c.id).limit(policy._PAGE)).all()
        if not rows:
            return
        yield from rows
        if len(rows) < policy._PAGE:
            return
        last = rows[-1].id
