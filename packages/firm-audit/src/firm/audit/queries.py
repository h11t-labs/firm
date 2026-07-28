"""Read-only queries over the audit log: paginated search, per-row and deployment-wide integrity.

These functions are a supported read surface — the dashboard (firm-ui) builds on it, and so can
your own dashboards, exporters, or health checks. Changing their signatures is a breaking change.

Unlike :func:`firm.audit.events.history`, :func:`audit_search` offers pagination and column
sorting (and pairs with :func:`audit_count` for a total), so it queries ``schema.audit_events``
directly rather than going through that helper. Filters take the same dual form ``history()``
accepts.

The tail of this module (:func:`verify_status_row`, :func:`integrity_config`,
:func:`integrity_state`, :func:`row_status`) reads the single ``firm_audit_verify_status`` row the
verifier upserts and folds it into a severity a caller can present. That derivation is pure, so the
whole state table is testable without a database; how a severity is *shown* (words, colours, icons)
is entirely the caller's business.

Input contract: a negative ``limit``/``offset`` raises ``ValueError``; an unknown ``sort`` or a
``dir`` outside ``{"asc", "desc"}`` falls back to :data:`DEFAULT_SORT` / descending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from .._core.clock import now_utc
from . import events, schema
from .events import Reference
from .verify import _STATUS_ID

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


def _check_window(limit: int, offset: int) -> None:
    """Reject a negative page window before it reaches SQL — the backends disagree about what a
    negative LIMIT/OFFSET means (SQLite reads a negative limit as "no limit"), so it is a caller
    bug worth naming rather than a silently different result set."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")


def audit_stats(conn: Connection) -> dict[str, Any]:
    """Log-wide totals: recorded ``events``, distinct ``actions``, and ``last_event_at``."""
    total = conn.execute(select(func.count()).select_from(_audits)).scalar_one()
    actions = conn.execute(
        select(func.count(func.distinct(_audits.c.action))).select_from(_audits)
    ).scalar_one()
    last_event_at = conn.execute(select(func.max(_audits.c.created_at))).scalar_one()
    return {"events": total, "actions": actions, "last_event_at": last_event_at}


def _event_dict(row: Any) -> dict[str, Any]:
    """One event row as a dict — exactly what :func:`firm.audit.events.history` returns, plus
    ``row_mac``: the Layer-1 signature (present once a key is configured, NULL on legacy/pre-key
    rows) that :func:`row_status` reads to tell "sealed/signed" apart from "unprotected"."""
    return events._row_to_dict(row) | {"row_mac": row.row_mac}


def _halves(
    name: str, ref: Reference, type_: str | None, id_: Any | None
) -> tuple[str | None, str | None]:
    """Resolve one reference field to its ``(type, id)`` halves, accepting either the paired
    ``subject=``/``actor=`` form or the split ``*_type=``/``*_id=`` one — the same contract as
    :func:`firm.audit.events.history`, including that passing both is a ``ValueError``."""
    if ref is not None and (type_ is not None or id_ is not None):
        raise ValueError(f"pass either {name}= or {name}_type=/{name}_id=, not both")
    if ref is not None:
        type_, id_, _ = events._ref(ref)
    return type_, None if id_ is None else str(id_)


def _apply_filters(
    stmt: Any,
    *,
    action: str | None,
    subject_type: str | None,
    subject_id: str | None,
    actor_type: str | None,
    actor_id: str | None,
    correlation_id: str | None,
) -> Any:
    """Shared by :func:`audit_search` and :func:`audit_count`, so the count always matches exactly
    the rows a search would return for the same filters. Each half filters independently, so
    ``subject_type="Invoice"`` alone matches every invoice regardless of id."""
    if action is not None:
        stmt = stmt.where(_audits.c.action == action)
    if subject_type is not None:
        stmt = stmt.where(_audits.c.subject_type == subject_type)
    if subject_id is not None:
        stmt = stmt.where(_audits.c.subject_id == subject_id)
    if actor_type is not None:
        stmt = stmt.where(_audits.c.actor_type == actor_type)
    if actor_id is not None:
        stmt = stmt.where(_audits.c.actor_id == actor_id)
    if correlation_id is not None:
        stmt = stmt.where(_audits.c.correlation_id == correlation_id)
    return stmt


def audit_count(
    conn: Connection,
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
    """The number of events matching these filters — for pagination, not the (unfiltered) log-wide
    total in :func:`audit_stats`. Filters are exactly :func:`audit_search`'s; see it for the two
    accepted reference forms."""
    subject_type, subject_id = _halves("subject", subject, subject_type, subject_id)
    actor_type, actor_id = _halves("actor", actor, actor_type, actor_id)
    stmt = _apply_filters(
        select(func.count()).select_from(_audits),
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    return conn.execute(stmt).scalar_one()


def audit_search(
    conn: Connection,
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
    """One page of matching events, as dicts (the ``history()`` shape plus ``row_mac``).

    ``subject``/``actor`` filter on the ``(type, id)`` of any accepted reference (a domain object,
    an explicit tuple, a :class:`firm.audit.Ref`, or a bare ``"label"`` string → **type only**, so
    ``subject="Invoice:42"`` filters on the *type* ``"Invoice:42"``, not on type + id — split it
    yourself, or pass ``("Invoice", 42)``, to filter the pair); ``subject_type``/``subject_id``/
    ``actor_type``/``actor_id`` filter on either half independently. Passing both forms for the
    same field is a ``ValueError``.

    ``sort`` is one of :data:`SORT_COLUMNS` and ``dir`` is ``"asc"``/``"desc"``; anything else
    falls back to :data:`DEFAULT_SORT` / descending rather than raising, so a stray query
    parameter degrades instead of erroring. Rows always carry an id tiebreaker, so paging is
    stable when the sort key ties.
    """
    _check_window(limit, offset)
    subject_type, subject_id = _halves("subject", subject, subject_type, subject_id)
    actor_type, actor_id = _halves("actor", actor, actor_type, actor_id)
    stmt = _apply_filters(
        select(_audits),
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )

    columns = SORT_COLUMNS.get(sort, SORT_COLUMNS[DEFAULT_SORT])
    order = [c.asc() for c in columns] if dir == "asc" else [c.desc() for c in columns]
    if sort != "id":
        order.append(_audits.c.id.desc())  # stable order across pages when the sort key ties
    stmt = stmt.order_by(*order).limit(limit).offset(offset)

    return [_event_dict(row) for row in conn.execute(stmt).all()]


def audit_detail(conn: Connection, event_id: int) -> dict[str, Any] | None:
    """One event by id — like :func:`firm.audit.events.get`, plus the ``row_mac`` that
    :func:`row_status` needs to classify it — or ``None`` when no such event exists."""
    row = conn.execute(select(_audits).where(_audits.c.id == event_id)).first()
    return None if row is None else _event_dict(row)


# -- per-row tamper-evidence status ------------------------------------------------------------
# The integrity *state* (below) reports the deployment-wide verdict; this pair reports the status
# of one audit row, so a row reads as sealed / signed-not-sealed / unprotected / tampered at a
# glance. :func:`row_integrity_context` gathers the two cheap signals once per page;
# :func:`row_status` is pure over a single row + that context, so the priority table is
# unit-testable without a database.


#: Hard cap on the ``affected_identifiers`` JSON these helpers will parse. The verifier bounds it
#: to :data:`firm.audit.verify._MAX_AFFECTED` small findings, so anything larger is corrupt or
#: hostile. Rejecting it before ``json.loads`` (and catching ``RecursionError`` below) keeps a
#: DB-write attacker from crashing every reader with an oversized or deeply-nested blob (Bug #3):
#: a dashboard re-parses this on every request, so an uncaught parse crash is a persistent DoS.
_MAX_AFFECTED_JSON = 64 * 1024


def _tampered_row_ids(raw: str | None) -> set[int]:
    """The integer row ids the latest verify run flagged as tampered, from its
    ``affected_identifiers`` JSON. Parses defensively — malformed/absent/oversized/deeply-nested
    data yields an empty set, never an exception (booleans are excluded even though ``bool`` is an
    ``int`` subclass)."""
    if not raw or len(raw) > _MAX_AFFECTED_JSON:
        return set()
    try:
        items = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
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


def _affected_is_truncated(raw: str | None) -> bool:
    """Whether the verifier truncated its ``affected_identifiers`` — i.e. more rows were flagged
    tampered than the JSON carries individual ids for (the ``kind="more"`` overflow marker). When it
    did, the set from :func:`_tampered_row_ids` is *incomplete*, so a sealed row not in it may
    still be one of the un-listed tampered rows — a reader must not vouch for it as verified
    (Bug #8)."""
    if not raw or len(raw) > _MAX_AFFECTED_JSON:
        return False
    try:
        items = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(items, list):
        return False
    return any(isinstance(i, dict) and i.get("kind") == "more" for i in items)


def row_integrity_context(conn: Connection) -> dict[str, Any]:
    """The signals :func:`row_status` needs, gathered once per page: whether tamper-evidence is in
    use at all (``active`` — any seal/activation/floor record exists or a verify run has happened),
    the newest covering seal's ``to_id`` (``max_sealed_to_id``, 0 when nothing is sealed), and the
    set of row ids the latest verify flagged tampered (``tampered_ids``). When ``active`` is False
    every row's status is ``None``, so a plain audit log carries no integrity dimension at all."""
    any_record = conn.execute(select(_seals.c.id).limit(1)).first() is not None
    max_to = conn.execute(select(func.max(_seals.c.to_id)).where(_seals.c.kind == "seal")).scalar()
    status = verify_status_row(conn)
    affected = status["affected_identifiers"] if status else None
    # When the latest run is tampered AND its affected list was truncated, the known tampered-id set
    # is incomplete — a sealed row not in it may still be one of the un-listed tampered rows, so it
    # must not be reported as verified (Bug #8).
    truncated = bool(
        status and status["outcome"] == "tampered" and _affected_is_truncated(affected)
    )
    return {
        "active": any_record or status is not None,
        "max_sealed_to_id": max_to or 0,
        "tampered_ids": _tampered_row_ids(affected),
        "tampered_truncated": truncated,
    }


def row_status(row: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    """One row's tamper-evidence status, or ``None`` when tamper-evidence is not in use (so the
    caller reports nothing). Priority, top wins: ``tampered`` (verify flagged this row id) >
    ``unprotected`` (no signature — a legacy pre-key row) > ``unsealed`` (signed but past the newest
    seal — the grace-window tail) > ``sealed`` (signed and within a seal).

    When the latest run flagged more tampered rows than its ``affected_identifiers`` lists ids for
    (``ctx["tampered_truncated"]`` — Bug #8), a sealed row not in the known set may still be one of
    the un-listed tampered rows, so it degrades to ``unverified`` (honest: "sealed, but this run
    could not vouch for it") instead of falsely reading ``sealed``."""
    if not ctx["active"]:
        return None
    if row["id"] in ctx["tampered_ids"]:
        return "tampered"
    if row["row_mac"] is None:
        return "unprotected"
    if row["id"] > ctx["max_sealed_to_id"]:
        return "unsealed"
    if ctx.get("tampered_truncated"):
        return "unverified"  # a truncated tamper run cannot vouch for this sealed row
    return "sealed"


# -- deployment-wide integrity state -----------------------------------------------------------
# The verifier (opt-in) upserts one ``firm_audit_verify_status`` row after each run; this tail
# reads it and folds it — together with whether integrity is switched on at all — into a single
# derived state with a severity. Nothing here presents anything: it only decides *which* of the six
# states applies, so the whole state table is unit-testable without a database.

# Liveness thresholds (seconds). Independent of the stored verdict, the derived state escalates to
# ``warn`` when the last verify run — or the newest anchor — is older than these, so a verify cron
# or anchor sink that quietly died surfaces within one threshold rather than ageing behind a stale
# ``ok``. Both are overridable by the caller.
DEFAULT_VERIFY_MAX_AGE = 24 * 60 * 60.0  # a nightly verify that skips a whole day goes to warn
DEFAULT_ANCHOR_MAX_AGE = 3 * 60.0  # 3x the 60s seal interval, matching the CLI's ``anchor_max_age``


@dataclass(frozen=True)
class IntegrityConfig:
    """Whether tamper-evidence is switched on for this deployment — the signal that tells
    "configured but never verified" apart from "no key at all". ``key_configured`` is supplied by
    the caller (the reading process's ``FIRM_AUDIT_KEY``), never inferred from whether a status row
    happens to exist; ``sealing_active`` / ``sealing_since`` come from the explicit signed
    activation marker written by the first sealer pass."""

    key_configured: bool
    sealing_active: bool
    sealing_since: datetime | None


@dataclass(frozen=True)
class IntegrityState:
    """The derived integrity state. ``tone`` is its severity (``ok``/``warn``/``danger``/
    ``neutral``, like log levels); ``escalate`` is whether this state deserves prominent surfacing
    beyond a dedicated integrity view (tampering and stalled-pipeline liveness do, a healthy state
    does not); ``causes`` are machine tokens (``stale``, ``sealer_stalled``, ``anchor_stale``,
    ``verify_warnings``) a caller can turn into itemized prose. ``status``/``config`` carry the raw
    values behind the verdict (timestamps, counts, the affected range);
    ``verify_max_age``/``anchor_max_age`` are the thresholds this state was derived under, so a
    caller can name them honestly."""

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
    ``None`` when verify has never run. Read by its fixed primary key
    (:data:`firm.audit.verify._STATUS_ID`) — the one canonical row the verifier upserts — **not**
    the newest by ``ran_at``. Ordering by ``ran_at`` would let a DB-write attacker insert a second,
    far-future ``outcome="ok"`` row and keep readers reporting a healthy log even after real
    verifies flagged tampering (Bug #2); the id-keyed read always reflects the last genuine verify
    run."""
    row = conn.execute(select(_verify_status).where(_verify_status.c.id == _STATUS_ID)).first()
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
        "newest_anchor_at": row.newest_anchor_at,
        "anchor_configured": row.anchor_configured,
        "unsealed_tail_count": row.unsealed_tail_count,
        "unsealed_tail_oldest_at": row.unsealed_tail_oldest_at,
        # JSON list of ``{"kind", "label", "id"?, "message"?, "verdict"}`` on tampering.
        "affected_identifiers": row.affected_identifiers,
        "duration_seconds": row.duration_seconds,
    }


def integrity_config(conn: Connection, *, key_configured: bool) -> IntegrityConfig:
    """Whether integrity is switched on. ``key_configured`` is the calling process's own
    ``FIRM_AUDIT_KEY`` presence (passed in, not read here); sealing state comes only from the
    explicit signed ``kind="activation"`` marker, whose ``sealed_at`` is the activation moment."""
    since = conn.execute(
        select(func.min(_seals.c.sealed_at)).where(_seals.c.kind == "activation")
    ).scalar_one()
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
    """Fold the status row + config into one derived state (pure — no I/O, so the whole state
    table is unit-tested directly). Priority: proven tampering dominates everything; then the
    "configured but never ran" vs "not configured" split on ``config`` (never on whether a status
    row exists); then verdict plus the liveness/anchor staleness that forces ``warn`` regardless of
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

    # Liveness / staleness — these force ``warn`` even over a stored ``ok``.
    causes: list[str] = []
    ran_age = _age(now, status["ran_at"])
    if ran_age is not None and ran_age > verify_max_age:
        causes.append("stale")
    tail_age = _age(now, status["unsealed_tail_oldest_at"])
    if tail_age is not None and tail_age > verify_max_age:
        causes.append("sealer_stalled")
    anchor_age = _age(now, status["newest_anchor_at"])
    # anchor-absent-by-design (``anchor_configured`` False) never reads as "stale".
    if status["anchor_configured"] and anchor_age is not None and anchor_age > anchor_max_age:
        causes.append("anchor_stale")
    if status["warning_count"]:
        causes.append("verify_warnings")

    if status["outcome"] == "error":
        # ERROR (verify itself failed, e.g. unknown key_id) is a warning that counts toward
        # liveness, so it escalates; ``danger`` stays reserved for proven tampering.
        return make("error", "warn", True, tuple(causes))
    if status["outcome"] == "warning" or causes:
        # Only a stalled pipeline (verify not running, sealer behind) escalates; a verifier or
        # stale-anchor warning stays in the integrity view.
        liveness = bool({"stale", "sealer_stalled"}.intersection(causes))
        return make("warning", "warn", liveness, tuple(causes))
    return make("ok", "ok", False)
