"""The :mod:`firm.audit.queries` read surface, over an :class:`~firm.audit.s3.AuditLog`.

Every function here has the same name, arguments and result shape as its SQL counterpart, with
the log in place of the connection, so a dashboard renders an S3 log exactly like a SQL one.
The integrity half (``row_integrity_context``, ``verify_status_row``, ``integrity_config``) reads
the same records the shared verifier writes; ``row_status``/``integrity_state`` are re-exported
unchanged because they are pure.

Cost model, stated rather than hidden: totals and single-filter counts read index keys only (one
LIST per 1,000 events). The default ``created_at``/``id`` sorts stream from an index, so a page
costs its own rows plus key reads for the skipped offset. Any other sort, or more than one filter,
reads the matching rows themselves and is limited to the newest :data:`SORT_WINDOW` matches.
"""

from __future__ import annotations

from datetime import datetime
from itertools import groupby
from typing import TYPE_CHECKING, Any

from .events import Reference
from .queries import (
    DEFAULT_SORT,
    SORT_COLUMNS,
    IntegrityConfig,
    IntegrityState,
    _check_window,
    _halves,
    _parse_affected,
    integrity_state,
    row_status,
)
from .s3_store import S3Storage, S3View, row_dict

if TYPE_CHECKING:
    from .log import AuditLog

__all__ = [
    "DEFAULT_SORT",
    "SORT_COLUMNS",
    "SORT_WINDOW",
    "IntegrityConfig",
    "IntegrityState",
    "audit_count",
    "audit_detail",
    "audit_search",
    "audit_stats",
    "integrity_config",
    "integrity_state",
    "row_integrity_context",
    "row_status",
    "verify_status_row",
]

#: How many matching rows a non-index sort (or a multi-filter search) reads, newest first.
SORT_WINDOW = 10_000


def _storage(log: AuditLog) -> S3Storage:
    if not isinstance(log.storage, S3Storage):
        raise ValueError("S3 queries require an AuditLog with S3Storage")
    return log.storage


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return row_dict(row) | {"row_mac": row.get("row_mac")}


def audit_stats(log: AuditLog) -> dict[str, Any]:
    """Log-wide totals: recorded ``events``, distinct ``actions``, and ``last_event_at``."""
    storage = _storage(log)
    actions = set()
    prefix = f"{storage.layout.root}ix/f/action/"
    for key in storage.store.iter_keys(prefix):
        actions.add(key[len(prefix) :].split("/", 1)[0])
    last = None
    for entry in storage.time_entries(None):
        rows = storage.rows([entry])
        if rows:
            last = datetime.fromisoformat(rows[0]["created_at"])
            break
    return {"events": storage.count_events(), "actions": len(actions), "last_event_at": last}


def _filters(
    *,
    action: str | None,
    subject: Reference,
    subject_type: str | None,
    subject_id: Any | None,
    actor: Reference,
    actor_type: str | None,
    actor_id: Any | None,
    correlation_id: str | None,
) -> dict[str, str]:
    subject_type, subject_id = _halves("subject", subject, subject_type, subject_id)
    actor_type, actor_id = _halves("actor", actor, actor_type, actor_id)
    values = {
        "action": action,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "correlation_id": correlation_id,
    }
    return {name: str(value) for name, value in values.items() if value is not None}


def _single_index(log: AuditLog, wanted: dict[str, str]) -> str | None:
    """The index prefix that holds exactly the rows matching ``wanted``, if one does."""
    layout = _storage(log).layout
    if len(wanted) == 1:
        ((name, value),) = wanted.items()
        return layout.field(name, value)
    if set(wanted) == {"subject_type", "subject_id"}:
        return layout.field("subject", f"{wanted['subject_type']}\x00{wanted['subject_id']}")
    if set(wanted) == {"actor_type", "actor_id"}:
        return layout.field("actor", f"{wanted['actor_type']}\x00{wanted['actor_id']}")
    return None


def audit_count(
    log: AuditLog,
    *,
    action: str | None = None,
    subject: Reference = None,
    subject_type: str | None = None,
    subject_id: Any | None = None,
    actor: Reference = None,
    actor_type: str | None = None,
    actor_id: Any | None = None,
    correlation_id: str | None = None,
) -> int:
    """The number of events matching these filters (see :func:`audit_search`)."""
    wanted = _filters(
        action=action,
        subject=subject,
        subject_type=subject_type,
        subject_id=subject_id,
        actor=actor,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    if not wanted:
        return _storage(log).count_events()
    prefix = _single_index(log, wanted)
    if prefix is not None:
        entries = list(_storage(log).index_entries(prefix))
        present = S3View(_storage(log), None)._existing_ids(event_id for event_id, _ in entries)
        return len(present)
    return len(_storage(log).matching(wanted, None, SORT_WINDOW))


def audit_search(
    log: AuditLog,
    *,
    action: str | None = None,
    subject: Reference = None,
    subject_type: str | None = None,
    subject_id: Any | None = None,
    actor: Reference = None,
    actor_type: str | None = None,
    actor_id: Any | None = None,
    correlation_id: str | None = None,
    sort: str = DEFAULT_SORT,
    dir: str = "desc",
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One page of matching events, as dicts (the ``history()`` shape plus ``row_mac``), with
    :func:`firm.audit.queries.audit_search`'s sort and tiebreak (id descending) rules."""
    _check_window(limit, offset)
    wanted = _filters(
        action=action,
        subject=subject,
        subject_type=subject_type,
        subject_id=subject_id,
        actor=actor,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    if sort not in SORT_COLUMNS:
        sort = DEFAULT_SORT
    if dir not in {"asc", "desc"}:
        dir = "desc"
    if limit == 0:
        return []
    storage = _storage(log)
    if not wanted and sort in {"id", "created_at"}:
        entries = _ordered_entries(log, sort, dir)
        page: list[tuple[int, str]] = []
        for index, entry in enumerate(entries):
            if index < offset:
                continue
            page.append(entry)
            if len(page) >= limit + 10:
                break
        rows = storage.rows(page)
        return [_event(row) for row in rows[:limit]]
    rows = _storage(log).matching(wanted, None, SORT_WINDOW)
    if sort == "id":
        rows.sort(key=lambda row: row["id"], reverse=dir == "desc")
    else:
        # Python's sort is stable even with ``reverse``: id descending stays the tiebreak.
        rows.sort(key=lambda row: -row["id"])
        rows.sort(key=lambda row: _sort_key(row, sort), reverse=dir == "desc")
    return [_event(row) for row in rows[offset : offset + limit]]


def _sort_key(row: dict[str, Any], sort: str) -> tuple[Any, ...]:
    if sort == "id":
        return (row["id"],)
    if sort == "created_at":
        return (row["created_at"],)
    if sort in {"subject", "actor"}:
        return tuple((value is not None, value or "") for value in _pair(row, sort))
    value = row.get(sort)
    return ((value is not None, value or ""),)  # NULLs first ascending, like SQLite/MySQL


def _pair(row: dict[str, Any], name: str) -> tuple[str | None, str | None]:
    return row.get(f"{name}_type"), row.get(f"{name}_id")


def _ordered_entries(log: AuditLog, sort: str, dir: str) -> Any:
    """``(id, entry_id)`` for every event in ``sort`` order, ties broken by id descending."""
    storage = _storage(log)
    layout = storage.layout
    if sort == "id":
        if dir == "desc":
            return storage.index_entries(layout.id_index)
        return (
            (event_id, entry_id)
            for event_id, entry_id, _ in S3View(storage, None)._event_keys()
            if event_id > 0
        )
    if dir == "desc":
        return storage.time_entries(None)  # created_at desc, then id desc

    def ascending() -> Any:
        # The ascending index orders equal timestamps by id ascending; the SQL tiebreak is id
        # descending, so each run of one timestamp is reversed.
        def stamped() -> Any:
            for key in storage.store.iter_keys(layout.time_asc):
                stamp, _, tail = key[len(layout.time_asc) :].partition("/")
                digits, _, entry = tail.partition("-")
                if digits.isdigit() and entry:
                    yield stamp, (int(digits), entry)

        for _, group in groupby(stamped(), key=lambda item: item[0]):
            yield from sorted((entry for _, entry in group), reverse=True)

    return ascending()


def audit_detail(log: AuditLog, event_id: int) -> dict[str, Any] | None:
    """One event by id, plus ``row_mac``, or ``None`` when no such event exists."""
    row = _storage(log).read_row(event_id)
    return None if row is None else _event(row)


def verify_status_row(log: AuditLog) -> dict[str, Any] | None:
    """The verify-status record the shared verifier writes, as the SQL helper returns it."""
    status = _storage(log).status()
    if status is None or "outcome" not in status:
        return None

    def when(name: str) -> datetime | None:
        value = status.get(name)
        return datetime.fromisoformat(value) if isinstance(value, str) else None

    return {
        "ran_at": when("ran_at"),
        "outcome": status.get("outcome"),
        "ok_count": status.get("ok_count", 0),
        "warning_count": status.get("warning_count", 0),
        "unprotected_count": status.get("unprotected_count", 0),
        "tampered_count": status.get("tampered_count", 0),
        "error_message": status.get("error_message"),
        "last_full_coverage_at": when("last_full_coverage_at"),
        "newest_anchor_at": when("newest_anchor_at"),
        "anchor_configured": bool(status.get("anchor_configured")),
        "unsealed_tail_count": status.get("unsealed_tail_count", 0),
        "unsealed_tail_oldest_at": when("unsealed_tail_oldest_at"),
        "affected_identifiers": status.get("affected_identifiers"),
        "duration_seconds": status.get("duration_seconds"),
    }


def row_integrity_context(log: AuditLog) -> dict[str, Any]:
    """The signals :func:`row_status` needs (see the SQL helper of the same name)."""
    records = S3View(_storage(log), None).seal_records()
    max_to = max((r.to_id for r in records if r.kind == "seal" and r.to_id is not None), default=0)
    status = verify_status_row(log)
    affected = status["affected_identifiers"] if status else None
    tampered_ids, truncated = _parse_affected(affected)
    return {
        "active": bool(records) or status is not None,
        "max_sealed_to_id": max_to,
        "tampered_ids": tampered_ids,
        "tampered_truncated": bool(status and status["outcome"] == "tampered" and truncated),
    }


def integrity_config(log: AuditLog, *, key_configured: bool) -> IntegrityConfig:
    """Whether integrity is switched on (sealing state from the signed activation record)."""
    activations = [
        r.sealed_at
        for r in S3View(_storage(log), None).seal_records()
        if r.kind == "activation" and r.sealed_at is not None
    ]
    since = min(activations, default=None)
    return IntegrityConfig(
        key_configured=key_configured, sealing_active=since is not None, sealing_since=since
    )
