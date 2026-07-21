"""Read-only queries for the audit part.

Unlike :func:`firm.audit.events.history`, ``audit_search`` here needs UI-shaped pagination and
column sorting, so it queries ``schema.audit_events`` directly rather than going through that
helper — the same division the queue/cache/channel query modules already draw against their parts.

The tail of this module (:func:`verify_status_row`, :func:`integrity_config`,
:func:`integrity_state`) feeds the dashboard's tamper-evidence panel (design review D22-D25). It
reads the single ``firm_audit_verify_status`` row the verifier upserts and derives the *display*
state the panel renders. That derivation is a pure function so the six state-table rows can be
unit-tested without a database; the presentation (prose, links, colours) stays in
:mod:`.render` / the ``Integrity`` component.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from firm._core.clock import now_utc
from firm.audit import events, schema
from firm.audit.serialization import load_json

_audits = schema.audit_events
_seals = schema.seals
_verify_status = schema.verify_status

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
        # Layer-1 signature — present once a key is configured, NULL on legacy/pre-key rows. Read
        # by :func:`row_status` to tell "sealed/signed" apart from "unprotected".
        "row_mac": row.row_mac,
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
    event = events.get(conn, event_id)
    if event is None:
        return None
    # ``events.get`` maps the display columns but not ``row_mac``; add it here (single-row, cheap)
    # so :func:`row_status` can classify the detail page without reaching back into firm-audit.
    event["row_mac"] = conn.execute(
        select(_audits.c.row_mac).where(_audits.c.id == event_id)
    ).scalar_one_or_none()
    return event


# -- per-row tamper-evidence status ------------------------------------------------------------
# The integrity *panel* (above) reports the deployment-wide verdict; this pair reports the status
# of one audit row for the events table / detail page, so a row reads as sealed / signed-not-sealed
# / unprotected / tampered at a glance. :func:`row_integrity_context` gathers the two cheap signals
# once per page; :func:`row_status` is pure over a single row + that context, so the priority table
# is unit-testable without a database.


def _tampered_row_ids(raw: str | None) -> set[int]:
    """The integer row ids the latest verify run flagged as tampered, from its
    ``affected_identifiers`` JSON. Parses defensively — malformed/absent data yields an empty set,
    never an exception (booleans are excluded even though ``bool`` is an ``int`` subclass)."""
    if not raw:
        return set()
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(items, list):
        return set()
    ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("verdict") != "tampered":
            continue
        id_ = item.get("id")
        if isinstance(id_, int) and not isinstance(id_, bool):
            ids.add(id_)
    return ids


def row_integrity_context(conn: Connection) -> dict[str, Any]:
    """The signals :func:`row_status` needs, gathered once per page: whether tamper-evidence is in
    use at all (``active`` — any seal exists or a verify run has happened), the newest sealed
    ``to_id`` (``max_sealed_to_id``, 0 when nothing is sealed), and the set of row ids the latest
    verify flagged tampered (``tampered_ids``). When ``active`` is False the table adds no status
    column at all, so a plain audit log looks exactly as it did before tamper-evidence existed."""
    max_to = conn.execute(select(func.max(_seals.c.to_id))).scalar()
    status = verify_status_row(conn)
    return {
        "active": max_to is not None or status is not None,
        "max_sealed_to_id": max_to or 0,
        "tampered_ids": _tampered_row_ids(status["affected_identifiers"]) if status else set(),
    }


def row_status(row: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    """One row's tamper-evidence status, or ``None`` when tamper-evidence is not in use (so the
    caller renders nothing). Priority, top wins: ``tampered`` (verify flagged this row id) >
    ``unprotected`` (no signature — a legacy pre-key row) > ``unsealed`` (signed but past the newest
    seal — the grace-window tail) > ``sealed`` (signed and within a seal)."""
    if not ctx["active"]:
        return None
    if row["id"] in ctx["tampered_ids"]:
        return "tampered"
    if row["row_mac"] is None:
        return "unprotected"
    if row["id"] > ctx["max_sealed_to_id"]:
        return "unsealed"
    return "sealed"


# -- integrity (tamper-evidence) panel ---------------------------------------------------------
# The verifier (opt-in) upserts one ``firm_audit_verify_status`` row after each run; this tail
# reads it and folds it — together with whether integrity is switched on at all — into the single
# display state the panel renders (design review D22-D25). Nothing here presents anything: the
# prose, links, and colours live in :mod:`.render`; this layer only decides *which* of the six
# states applies, so the whole state table is unit-testable without a database.

# Liveness thresholds (seconds). Independent of the stored verdict, the panel forces amber when
# the last verify run — or the newest anchor — is older than these, so a verify cron or anchor
# sink that quietly died surfaces within one threshold rather than ageing behind a stale green
# (design "Staleness" / review D16). Both are overridable by the caller.
DEFAULT_VERIFY_MAX_AGE = 24 * 60 * 60.0  # a nightly verify that skips a whole day goes amber
DEFAULT_ANCHOR_MAX_AGE = 3 * 60.0  # 3x the 60s seal interval, matching the CLI's ``anchor_max_age``


@dataclass(frozen=True)
class IntegrityConfig:
    """Whether tamper-evidence is switched on for this deployment — the signal that tells
    "configured but never verified" apart from "no key at all" (design D22). ``key_configured``
    is supplied by the *server context* (the dashboard process's ``FIRM_AUDIT_KEY``), never
    inferred from whether a status row happens to exist; ``sealing_active`` / ``sealing_since``
    come from the seal chain, since a running sealer is what stamps the first seal and dates the
    activation the panel's "active since N" line names."""

    key_configured: bool
    sealing_active: bool
    sealing_since: datetime | None


@dataclass(frozen=True)
class IntegrityState:
    """The derived display state — one of the six rows of the design's state table. ``tone`` is
    the pill/strip colour token (``ok``/``warn``/``danger``/``neutral``); ``escalate`` is whether
    this state also renders at the top of the overview page (TAMPERED and amber-liveness do, the
    calm OK strip does not — review D23); ``causes`` are machine tokens (``stale``,
    ``sealer_stalled``, ``anchor_stale``, ``late_commits``) that :mod:`.render` turns into the
    itemized WARNING/ERROR prose. ``status``/``config`` carry the raw values render reads for
    timestamps, counts, and affected-range links; ``verify_max_age``/``anchor_max_age`` are the
    thresholds this state was derived under, so the prose can name them honestly."""

    state: str
    tone: str
    escalate: bool
    causes: tuple[str, ...]
    status: dict[str, Any] | None
    config: IntegrityConfig
    verify_max_age: float
    anchor_max_age: float


def verify_status_row(conn: Connection) -> dict[str, Any] | None:
    """The single ``firm_audit_verify_status`` row the verifier upserts, as a plain dict, or
    ``None`` when verify has never run. "Single row" is the writer's contract, not a schema
    constraint, so we order by ``ran_at`` and take the newest — a stale leftover never wins."""
    row = conn.execute(select(_verify_status).order_by(_verify_status.c.ran_at.desc())).first()
    if row is None:
        return None
    return {
        "ran_at": row.ran_at,
        "outcome": row.outcome,
        "ok_count": row.ok_count,
        "warning_count": row.warning_count,
        "unprotected_count": row.unprotected_count,
        "tampered_count": row.tampered_count,
        "error_message": row.error_message,
        "last_full_coverage_at": row.last_full_coverage_at,
        "cycle_position": row.cycle_position,
        "cycle_length": row.cycle_length,
        "newest_anchor_at": row.newest_anchor_at,
        "anchor_configured": row.anchor_configured,
        "unsealed_tail_count": row.unsealed_tail_count,
        "unsealed_tail_oldest_at": row.unsealed_tail_oldest_at,
        # JSON list of ``{"kind", "label", "id"?, "message"?, "verdict"}`` on tampering; see
        # ``render._affected_cells``.
        "affected_identifiers": row.affected_identifiers,
        "duration_seconds": row.duration_seconds,
    }


def integrity_config(conn: Connection, *, key_configured: bool) -> IntegrityConfig:
    """Whether integrity is switched on. ``key_configured`` is the server's own
    ``FIRM_AUDIT_KEY`` presence (passed in, not read here); sealing state comes from the seal
    chain — ``sealing_since`` is the oldest seal's ``sealed_at``, i.e. the activation moment."""
    since = conn.execute(select(func.min(_seals.c.sealed_at))).scalar_one()
    return IntegrityConfig(
        key_configured=key_configured, sealing_active=since is not None, sealing_since=since
    )


def _age(now: datetime, value: datetime | None) -> float | None:
    """Seconds between ``now`` and ``value`` (both timezone-naive UTC per :func:`now_utc`), or
    ``None`` when ``value`` is absent."""
    return None if value is None else (now - value).total_seconds()


def integrity_state(
    status: dict[str, Any] | None,
    config: IntegrityConfig,
    *,
    now: datetime | None = None,
    verify_max_age: float = DEFAULT_VERIFY_MAX_AGE,
    anchor_max_age: float = DEFAULT_ANCHOR_MAX_AGE,
) -> IntegrityState:
    """Fold the status row + config into a display state (pure — no I/O, so the whole state
    table is unit-tested directly). Priority: proven tampering dominates everything; then the
    "configured but never ran" vs "not configured" split on ``config`` (never on whether a status
    row exists); then verdict plus the liveness/anchor staleness that forces amber regardless of
    the stored verdict (a dead verify cron cannot record its own death)."""
    now = now or now_utc()
    configured = config.key_configured or config.sealing_active

    def make(state: str, tone: str, escalate: bool, causes: tuple[str, ...] = ()) -> IntegrityState:
        return IntegrityState(
            state, tone, escalate, causes, status, config, verify_max_age, anchor_max_age
        )

    if status is None:
        return (
            make("never_ran", "warn", True)
            if configured
            else make("not_configured", "neutral", False)
        )

    if status["outcome"] == "tampered" or status["tampered_count"]:
        return make("tampered", "danger", True)

    # Liveness / staleness — these force amber even over a stored ``ok`` (design "Staleness").
    causes: list[str] = []
    ran_age = _age(now, status["ran_at"])
    if ran_age is not None and ran_age > verify_max_age:
        causes.append("stale")
    tail_age = _age(now, status["unsealed_tail_oldest_at"])
    if tail_age is not None and tail_age > verify_max_age:
        causes.append("sealer_stalled")
    anchor_age = _age(now, status["newest_anchor_at"])
    # anchor-absent-by-design (``anchor_configured`` False) never reads as "stale" (review D22).
    if status["anchor_configured"] and anchor_age is not None and anchor_age > anchor_max_age:
        causes.append("anchor_stale")
    if status["warning_count"]:
        causes.append("late_commits")

    if status["outcome"] == "error":
        # ERROR (verify itself failed, e.g. unknown key_id) is amber and counts toward liveness
        # so it escalates; red stays reserved for proven tampering (design D24).
        return make("error", "warn", True, tuple(causes))
    if status["outcome"] == "warning" or causes:
        # Only a stalled pipeline (verify not running, sealer behind) is "amber liveness" and
        # escalates to the overview; a benign late-commit / stale-anchor warning stays on the
        # audit tab (review D23).
        liveness = bool({"stale", "sealer_stalled"}.intersection(causes))
        return make("warning", "warn", liveness, tuple(causes))
    return make("ok", "ok", False)
