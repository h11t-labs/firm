"""Specs for the integrity read helpers in :mod:`firm.audit.queries`.

Two layers:

* the pure derivations (:func:`row_status`, :func:`integrity_state`) are asserted directly — the
  four per-row states plus the six deployment-wide ones, without a database;
* the query helpers (:func:`row_integrity_context`, :func:`verify_status_row`,
  :func:`integrity_config`) run against a real (seeded) audit schema.

How any of it is *presented* is the caller's business; the dashboard's own rendering is specced in
``tests/ui``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert

from firm._core.clock import now_utc
from firm.audit import AuditLog, schema
from firm.audit.queries import (
    _MAX_AFFECTED_JSON,
    IntegrityConfig,
    _tampered_row_ids,
    integrity_config,
    integrity_state,
    row_integrity_context,
    row_status,
    verify_status_row,
)

NOW = datetime(2026, 7, 20, 12, 0, 0)
_MAC = "ab" * 32


def seal(
    audit: AuditLog,
    *,
    kind: str = "seal",
    from_id: int | None = 0,
    to_id: int = 1,
    row_count: int | None = 1,
    rows_mac: str | None = "00" * 32,
    sealed_at: datetime | None = None,
) -> None:
    """A ``firm_audit_seals`` record with harmless placeholder MACs — these specs only read the
    kinds and ranges, never re-verify the signatures."""
    with audit.engine.begin() as conn:
        conn.execute(
            insert(schema.seals).values(
                kind=kind,
                from_id=from_id,
                to_id=to_id,
                row_count=row_count,
                rows_mac=rows_mac,
                seal_mac="ab" * 32,
                sealed_at=sealed_at or now_utc(),
                key_id="deadbeef",
            )
        )


def verify_status(
    audit: AuditLog,
    *,
    outcome: str = "ok",
    ran_at: datetime | None = None,
    ok_count: int = 0,
    warning_count: int = 0,
    unprotected_count: int = 0,
    tampered_count: int = 0,
    error_message: str | None = None,
    last_full_coverage_at: datetime | None = None,
    newest_anchor_at: datetime | None = None,
    anchor_configured: bool = False,
    unsealed_tail_count: int = 0,
    unsealed_tail_oldest_at: datetime | None = None,
    affected_identifiers: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    """The single ``firm_audit_verify_status`` row the verifier would upsert."""
    with audit.engine.begin() as conn:
        conn.execute(
            insert(schema.verify_status).values(
                ran_at=ran_at or now_utc(),
                outcome=outcome,
                ok_count=ok_count,
                warning_count=warning_count,
                unprotected_count=unprotected_count,
                tampered_count=tampered_count,
                error_message=error_message,
                last_full_coverage_at=last_full_coverage_at,
                newest_anchor_at=newest_anchor_at,
                anchor_configured=anchor_configured,
                unsealed_tail_count=unsealed_tail_count,
                unsealed_tail_oldest_at=unsealed_tail_oldest_at,
                affected_identifiers=affected_identifiers,
                duration_seconds=duration_seconds,
            )
        )


def _ctx(
    *,
    active: bool = True,
    max_sealed: int = 0,
    tampered: set[int] | None = None,
    truncated: bool = False,
) -> dict:
    return {
        "active": active,
        "max_sealed_to_id": max_sealed,
        "tampered_ids": tampered or set(),
        "tampered_truncated": truncated,
    }


# -- row_status: the four states + None-when-inactive -------------------------------------------


def test_row_status_none_when_inactive() -> None:
    # A plain audit log (no key, no seals, no verify) reports no status at all.
    assert row_status({"id": 1, "row_mac": _MAC}, _ctx(active=False)) is None


def test_row_status_sealed_within_seal_range() -> None:
    assert row_status({"id": 3, "row_mac": _MAC}, _ctx(max_sealed=5)) == "sealed"


def test_row_status_unsealed_past_the_newest_seal() -> None:
    # Signed but beyond the newest sealed id — the grace-window tail.
    assert row_status({"id": 9, "row_mac": _MAC}, _ctx(max_sealed=5)) == "unsealed"


def test_row_status_unprotected_when_no_row_mac() -> None:
    assert row_status({"id": 1, "row_mac": None}, _ctx(max_sealed=5)) == "unprotected"


def test_row_status_tampered_dominates_a_sealed_row() -> None:
    assert row_status({"id": 3, "row_mac": _MAC}, _ctx(max_sealed=5, tampered={3})) == "tampered"


def test_row_status_degrades_to_unverified_when_findings_truncated() -> None:
    # Bug #8. A tamper run flagged more rows than its affected list carries ids for (truncated). A
    # sealed row NOT in the known tampered set could still be one of the un-listed tampered rows, so
    # it must not read as verified — it degrades to the honest "unverified".
    ctx = _ctx(max_sealed=5, tampered={2}, truncated=True)
    assert row_status({"id": 4, "row_mac": _MAC}, ctx) == "unverified"  # sealed, but unvouched
    # A row that IS in the known tampered set still reads tampered (priority unchanged).
    assert row_status({"id": 2, "row_mac": _MAC}, ctx) == "tampered"
    # Without truncation the same sealed row reads sealed & verified.
    assert row_status({"id": 4, "row_mac": _MAC}, _ctx(max_sealed=5, tampered={2})) == "sealed"


def test_row_status_tampered_dominates_an_unprotected_row() -> None:
    # Priority is tampered first, before the row_mac-null check.
    assert row_status({"id": 3, "row_mac": None}, _ctx(max_sealed=5, tampered={3})) == "tampered"


# -- row_integrity_context: gathered once per page ---------------------------------------------


def test_context_inactive_without_seals_or_verify(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["active"] is False
    assert ctx["max_sealed_to_id"] == 0
    assert ctx["tampered_ids"] == set()


def test_context_active_from_a_seal(audit: AuditLog) -> None:
    seal(audit, to_id=7)
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["active"] is True
    assert ctx["max_sealed_to_id"] == 7


def test_context_activation_is_active_but_not_sealed_coverage(audit: AuditLog) -> None:
    seal(audit, kind="activation", from_id=-1, to_id=7, row_count=None, rows_mac=None)
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["active"] is True
    assert ctx["max_sealed_to_id"] == 0


def test_context_active_from_a_verify_row_alone(audit: AuditLog) -> None:
    verify_status(audit, outcome="ok")
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["active"] is True


def test_context_collects_tampered_row_ids(audit: AuditLog) -> None:
    affected = (
        '[{"kind": "row", "label": "row 42", "id": 42, "verdict": "tampered"},'
        '{"kind": "seal", "label": "seal 3", "verdict": "tampered"},'
        '{"kind": "row", "label": "row 7", "id": 7, "verdict": "ok"}]'
    )
    verify_status(audit, outcome="tampered", tampered_count=1, affected_identifiers=affected)
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["tampered_ids"] == {42}  # only the tampered finding with an integer id


def test_context_tampered_ids_survives_malformed_json(audit: AuditLog) -> None:
    verify_status(audit, outcome="tampered", tampered_count=1, affected_identifiers="{not json")
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["tampered_ids"] == set()


def test_context_flags_truncated_findings(audit: AuditLog) -> None:
    # Bug #8. A tampered run whose affected list carries the "more" overflow marker sets
    # tampered_truncated, so callers stop vouching for un-listed sealed rows.
    affected = (
        '[{"kind": "row", "label": "row 1", "id": 1, "verdict": "tampered"},'
        '{"kind": "more", "label": "+40 more finding(s)", "verdict": "tampered"}]'
    )
    verify_status(audit, outcome="tampered", tampered_count=41, affected_identifiers=affected)
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["tampered_truncated"] is True
    assert ctx["tampered_ids"] == {1}


def test_context_not_truncated_without_the_more_marker(audit: AuditLog) -> None:
    affected = '[{"kind": "row", "label": "row 1", "id": 1, "verdict": "tampered"}]'
    verify_status(audit, outcome="tampered", tampered_count=1, affected_identifiers=affected)
    with audit.engine.connect() as conn:
        ctx = row_integrity_context(conn)
    assert ctx["tampered_truncated"] is False


def test_tampered_row_ids_survives_deeply_nested_json() -> None:
    # Bug #3. A DB-write attacker could set affected_identifiers to a deeply-nested JSON blob;
    # json.loads raises RecursionError (not ValueError/TypeError), which used to 500 the audit page
    # on every render (parsed twice per request). It must degrade to an empty set instead.
    deep = "[" * 5000 + "]" * 5000
    assert _tampered_row_ids(deep) == set()


def test_tampered_row_ids_rejects_oversized_json() -> None:
    # An oversized blob is rejected before json.loads — the verifier only ever writes a handful of
    # small findings, so anything past the cap is corrupt or hostile.
    huge = '[{"kind":"row","id":1,"verdict":"tampered"}]' + " " * _MAX_AFFECTED_JSON
    assert _tampered_row_ids(huge) == set()


# -- integrity_state: the six state-table rows -------------------------------------------------


def _cfg(
    *, key: bool = True, sealing: bool = True, since: datetime | None = None
) -> IntegrityConfig:
    return IntegrityConfig(
        key_configured=key,
        sealing_active=sealing,
        sealing_since=since or datetime(2026, 7, 1, 0, 0, 0),
    )


def _status(**over: object) -> dict[str, object]:
    """A healthy, freshly-verified status row; override individual fields per test."""
    base: dict[str, object] = {
        "ran_at": NOW - timedelta(minutes=5),
        "outcome": "ok",
        "ok_count": 10,
        "warning_count": 0,
        "unprotected_count": 0,
        "tampered_count": 0,
        "error_message": None,
        "last_full_coverage_at": NOW - timedelta(hours=6),
        "newest_anchor_at": NOW - timedelta(seconds=30),
        "anchor_configured": True,
        "unsealed_tail_count": 2,
        "unsealed_tail_oldest_at": NOW - timedelta(seconds=30),
        "affected_identifiers": None,
        "duration_seconds": 1.2,
    }
    base.update(over)
    return base


def test_state_not_configured_when_no_key_and_no_status() -> None:
    st = integrity_state(None, _cfg(key=False, sealing=False), now=NOW)
    assert st.state == "not_configured"
    assert st.tone == "neutral"
    assert st.escalate is False


def test_state_never_ran_when_configured_but_no_status() -> None:
    st = integrity_state(None, _cfg(key=True, sealing=True), now=NOW)
    assert st.state == "never_ran"
    assert st.tone == "warn"
    assert st.escalate is True  # a stalled pipeline deserves prominent surfacing


def test_state_ok_when_fresh_and_clean() -> None:
    st = integrity_state(_status(), _cfg(), now=NOW)
    assert st.state == "ok"
    assert st.tone == "ok"
    assert st.escalate is False
    assert st.causes == ()


def test_state_verifier_warning_does_not_escalate() -> None:
    st = integrity_state(_status(outcome="warning", warning_count=2), _cfg(), now=NOW)
    assert st.state == "warning"
    assert "verify_warnings" in st.causes
    assert st.escalate is False  # a non-liveness warning stays in the integrity view


def test_state_error_is_a_warning_and_escalates() -> None:
    st = integrity_state(
        _status(outcome="error", error_message="unknown key_id ab12"), _cfg(), now=NOW
    )
    assert st.state == "error"
    assert st.tone == "warn"  # danger stays reserved for proven tampering
    assert st.escalate is True


def test_state_tampered_dominates_everything() -> None:
    st = integrity_state(_status(outcome="tampered", tampered_count=3), _cfg(), now=NOW)
    assert st.state == "tampered"
    assert st.tone == "danger"
    assert st.escalate is True


def test_tampered_count_alone_forces_tampered() -> None:
    # A stored outcome that lags the counts must not soften a real finding.
    st = integrity_state(_status(outcome="ok", tampered_count=1), _cfg(), now=NOW)
    assert st.state == "tampered"


def test_verify_max_age_forces_a_warning_over_a_stored_ok() -> None:
    old = _status(ran_at=NOW - timedelta(hours=30))  # a nightly cron that skipped a day
    st = integrity_state(old, _cfg(), now=NOW, verify_max_age=24 * 3600.0)
    assert st.state == "warning"
    assert "stale" in st.causes
    assert st.escalate is True  # a dead verify cron is a liveness alarm


def test_fresh_run_within_max_age_stays_ok() -> None:
    st = integrity_state(_status(ran_at=NOW - timedelta(hours=1)), _cfg(), now=NOW)
    assert st.state == "ok"


def test_anchor_absent_by_design_never_reads_as_stale() -> None:
    # No anchor configured: an aggressive threshold must not manufacture an "anchor stale" cause.
    st = integrity_state(
        _status(anchor_configured=False, newest_anchor_at=None),
        _cfg(),
        now=NOW,
        anchor_max_age=1.0,
    )
    assert st.state == "ok"
    assert "anchor_stale" not in st.causes


def test_configured_anchor_past_threshold_warns() -> None:
    st = integrity_state(
        _status(anchor_configured=True, newest_anchor_at=NOW - timedelta(hours=1)),
        _cfg(),
        now=NOW,
        anchor_max_age=60.0,
    )
    assert st.state == "warning"
    assert "anchor_stale" in st.causes
    assert st.escalate is False  # a lagging anchor sink is not a pipeline-liveness alarm


def test_stalled_sealer_escalates() -> None:
    st = integrity_state(
        _status(unsealed_tail_oldest_at=NOW - timedelta(hours=30), unsealed_tail_count=500),
        _cfg(),
        now=NOW,
        verify_max_age=24 * 3600.0,
    )
    assert st.state == "warning"
    assert "sealer_stalled" in st.causes
    assert st.escalate is True


# -- the status/config query helpers -----------------------------------------------------------


def test_verify_status_row_none_when_never_run(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        assert verify_status_row(conn) is None


def test_verify_status_row_reads_upserted_row(audit: AuditLog) -> None:
    verify_status(audit, outcome="warning", warning_count=4, unsealed_tail_count=7)
    with audit.engine.connect() as conn:
        row = verify_status_row(conn)
    assert row is not None
    assert row["outcome"] == "warning"
    assert row["warning_count"] == 4
    assert row["unsealed_tail_count"] == 7


def test_verify_status_row_reads_canonical_id_not_newest_ran_at(audit: AuditLog) -> None:
    # Bug #2. The verifier upserts a single fixed row (id 1). A DB-write attacker cannot flip the
    # verdict by inserting a SECOND, far-future ``outcome="ok"`` row: the canonical row is read by
    # id, never the newest by ``ran_at``. The first seeded row is the genuine tampered verify at
    # id 1; the second is the attacker's future-dated forgery at id 2.
    verify_status(audit, outcome="tampered", tampered_count=1, ran_at=NOW)  # id 1 — the real one
    verify_status(audit, outcome="ok", ran_at=NOW + timedelta(days=3650))  # id 2 — the forgery
    with audit.engine.connect() as conn:
        row = verify_status_row(conn)
    assert row is not None
    assert row["outcome"] == "tampered"  # the canonical row wins — the forgery is ignored
    assert row["tampered_count"] == 1


def test_integrity_config_without_seals_is_inactive(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        cfg = integrity_config(conn, key_configured=False)
    assert cfg.key_configured is False
    assert cfg.sealing_active is False
    assert cfg.sealing_since is None


def test_integrity_config_reads_explicit_activation_marker(audit: AuditLog) -> None:
    older_seal = NOW - timedelta(days=2)
    activated_at = NOW - timedelta(days=1)
    seal(audit, from_id=0, to_id=5, sealed_at=older_seal)
    seal(
        audit,
        kind="activation",
        from_id=-1,
        to_id=0,
        row_count=None,
        rows_mac=None,
        sealed_at=activated_at,
    )
    seal(audit, from_id=5, to_id=10, sealed_at=NOW)
    with audit.engine.connect() as conn:
        cfg = integrity_config(conn, key_configured=True)
    assert cfg.key_configured is True
    assert cfg.sealing_active is True
    assert cfg.sealing_since == activated_at
