"""Read-only queries for the audit part.

Unlike :func:`firm.audit.events.history`, ``audit_search`` here needs UI-shaped pagination and
column sorting, so it queries ``schema.audits`` directly rather than going through that helper —
the same division the queue/cache/channel query modules already draw against their own parts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from firm.audit import events, schema
from firm.audit.serialization import load_json

_audits = schema.audits

# Sortable columns, in table order. Each maps to one or more real columns (composite for
# subject/actor, so e.g. sorting by "subject" groups same-type rows together); always a plain
# allowlist lookup, never user input reaching SQL directly.
SORT_COLUMNS: dict[str, tuple[Any, ...]] = {
    "id": (_audits.c.id,),
    "created_at": (_audits.c.created_at,),
    "action": (_audits.c.action,),
    "subject": (_audits.c.subject_type, _audits.c.subject_id),
    "actor": (_audits.c.actor_type, _audits.c.actor_id),
    "correlation_id": (_audits.c.correlation_id,),
}
DEFAULT_SORT = "created_at"


def audit_stats(conn: Connection) -> dict[str, Any]:
    total = conn.execute(select(func.count()).select_from(_audits)).scalar_one()
    actions = conn.execute(
        select(func.count(func.distinct(_audits.c.action))).select_from(_audits)
    ).scalar_one()
    last_event_at = conn.execute(select(func.max(_audits.c.created_at))).scalar_one()
    return {"events": total, "actions": actions, "last_event_at": last_event_at}


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "subject_label": row.subject_label,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "actor_label": row.actor_label,
        "correlation_id": row.correlation_id,
        "data": load_json(row.data),
        "created_at": row.created_at,
    }


def _apply_filters(
    stmt: Any,
    *,
    action: str | None,
    subject: str | None,
    actor: str | None,
    correlation_id: str | None,
) -> Any:
    """Shared by :func:`audit_search` and :func:`audit_count`, so the count always matches
    exactly the rows a search would return for the same filters. ``subject``/``actor`` are a
    single ``"Type:id"`` string (matching how they're displayed and linked elsewhere); either half
    may be empty — ``"cron"`` (no colon) filters by type alone, ``"Invoice:42"`` filters the pair —
    so label-only refs (which have no id) stay filterable."""
    if action:
        stmt = stmt.where(_audits.c.action == action)
    if subject:
        subject_type, _, subject_id = subject.partition(":")
        if subject_type:
            stmt = stmt.where(_audits.c.subject_type == subject_type)
        if subject_id:
            stmt = stmt.where(_audits.c.subject_id == subject_id)
    if actor:
        actor_type, _, actor_id = actor.partition(":")
        if actor_type:
            stmt = stmt.where(_audits.c.actor_type == actor_type)
        if actor_id:
            stmt = stmt.where(_audits.c.actor_id == actor_id)
    if correlation_id:
        stmt = stmt.where(_audits.c.correlation_id == correlation_id)
    return stmt


def audit_count(
    conn: Connection,
    *,
    action: str | None = None,
    subject: str | None = None,
    actor: str | None = None,
    correlation_id: str | None = None,
) -> int:
    """The number of events matching these filters — for pagination, not the (unfiltered)
    dashboard-wide total in :func:`audit_stats`."""
    stmt = _apply_filters(
        select(func.count()).select_from(_audits),
        action=action,
        subject=subject,
        actor=actor,
        correlation_id=correlation_id,
    )
    return conn.execute(stmt).scalar_one()


def audit_search(
    conn: Connection,
    *,
    action: str | None = None,
    subject: str | None = None,
    actor: str | None = None,
    correlation_id: str | None = None,
    sort: str = DEFAULT_SORT,
    dir: str = "desc",
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    stmt = _apply_filters(
        select(_audits),
        action=action,
        subject=subject,
        actor=actor,
        correlation_id=correlation_id,
    )

    columns = SORT_COLUMNS.get(sort, SORT_COLUMNS[DEFAULT_SORT])
    order = [c.asc() for c in columns] if dir == "asc" else [c.desc() for c in columns]
    if sort != "id":
        order.append(_audits.c.id.desc())  # stable order across pages when the sort key ties
    stmt = stmt.order_by(*order).limit(limit).offset(offset)

    return [_row_to_dict(row) for row in conn.execute(stmt).all()]


def audit_detail(conn: Connection, event_id: int) -> dict[str, Any] | None:
    return events.get(conn, event_id)
