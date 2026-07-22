"""Verification: the four verdict classes, the full tamper matrix, rolling coverage, the anchor
check, the status-row upsert, and the CLI exit codes.

Runs on SQLite by default and on Postgres/MySQL when their ``FIRM_TEST_*`` URLs are set. The tamper
tests use ``full=True`` so the edited range is always recomputed; the rolling-coverage test
exercises the default (non-full) rotation instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, Ref, schema
from firm.audit.integrity import (
    canonical_gaps,
    gaps_for_range,
    load_key,
    new_ulid,
    row_mac,
    rows_mac,
    seal_mac,
)
from firm.audit.verify import IntegrityAlert, VerifyError

_SECRET = "verify-secret-key-padding-0123456789ab"  # noqa: S105  (>= 32 chars, throwaway)
_KEY = load_key(_SECRET)
assert _KEY is not None

_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FIRM_AUDIT_KEY",
        "FIRM_AUDIT_RETIRED_KEYS",
        "FIRM_AUDIT_RETIRED_SEAL_KEYS",
        "FIRM_AUDIT_ANCHOR_PATH",
        "FIRM_AUDIT_VERIFY_STATE",
    ):
        monkeypatch.delenv(var, raising=False)


# -- helpers ------------------------------------------------------------------------------------


def _make(db_url: str, **kw) -> AuditLog:
    return AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, **kw)


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _status_row(engine):
    with transaction(engine) as conn:
        return conn.execute(select(_status)).first()


def _insert_manual_seal(engine, *, seq, from_id, to_id, pairs, row_count, prev_mac, kind="seal"):
    """Insert a hand-built (still key-signed) seal — for simulating late commits and edits.

    ``gap_ranges`` is derived from the covered ``pairs`` so a seal that models a late commit (it
    counts fewer ids than the range spans) records the skipped id as a gap, exactly as the real
    sealer would (Bug #1)."""
    sealed_at = now_utc()
    rmac = rows_mac(_KEY, pairs)
    gaps = canonical_gaps(gaps_for_range(from_id, to_id, [rid for rid, _ in pairs]))
    smac = seal_mac(
        _KEY,
        seq=seq,
        kind=kind,
        from_id=from_id,
        to_id=to_id,
        row_count=row_count,
        rows_mac=rmac,
        prev_mac=prev_mac,
        sealed_at=sealed_at,
        gaps=gaps,
    )
    with transaction(engine) as conn:
        conn.execute(
            _seals.insert().values(
                seq=seq,
                kind=kind,
                from_id=from_id,
                to_id=to_id,
                row_count=row_count,
                rows_mac=rmac,
                prev_mac=prev_mac,
                seal_mac=smac,
                sealed_at=sealed_at,
                key_id=_KEY.id,
                gap_ranges=gaps or None,
            )
        )


# -- happy paths --------------------------------------------------------------------------------


def test_empty_log_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.tampered_count == 0
        assert report.unsealed_tail_count == 0
    finally:
        audit.close()


def test_single_row_sealed_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("only")
        audit.sealer.run_once()
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.ok_count == 1
    finally:
        audit.close()


def test_all_sealed_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for i in range(5):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.ok_count == 5
        assert report.unsealed_tail_count == 0
    finally:
        audit.close()


def test_unsealed_tail_rows_are_ok_and_counted(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.record("b")  # unsealed tail
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unsealed_tail_count == 1
    finally:
        audit.close()


def test_legacy_only_log_is_unprotected_not_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        # Rows written before the key existed carry no MAC; with no seal there is no activation
        # boundary, so they are legacy/unprotected, not tampering.
        with transaction(audit.engine) as conn:
            for a in ("x", "y", "z"):
                conn.execute(_audits.insert().values(action=a, created_at=now_utc()))
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.unprotected_count == 3
        assert report.tampered_count == 0
    finally:
        audit.close()


def test_batched_first_seal_of_legacy_backlog_is_unprotected_not_tampered(db_url: str) -> None:
    # A legacy backlog larger than seal_batch_size: the initial drain becomes several seals, so
    # seq 1 only reaches the end of the *first* batch (review 7A). The activation boundary must be
    # the highest sealed id, not seq 1's to_id — otherwise every legacy row batched into seq 2+
    # verifies as TAMPERED, a false red on a two-phase rollout (D13).
    audit = _make(db_url, seal_batch_size=2)
    try:
        with transaction(audit.engine) as conn:
            for i in range(5):
                conn.execute(_audits.insert().values(action=f"legacy{i}", created_at=now_utc()))
        audit.sealer.run_once()  # seals (0,2], (2,4], (4,5]
        assert len(_seal_all(audit.engine)) == 3
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.tampered_count == 0
        assert report.unprotected_count == 5  # every legacy row is unprotected, none tampered
    finally:
        audit.close()


def test_null_mac_row_slipped_into_sealed_legacy_range_is_tampered(db_url: str) -> None:
    # An out-of-band NULL-MAC row inserted into a sealed legacy range (reusing a rollback-gap id at
    # or below the activation boundary) must be TAMPERED, never downgraded to a valid-MAC
    # late-commit WARNING: a late commit is a *validly signed* extra row; a NULL-MAC extra row that
    # makes the range's count/rows_mac diverge is a forged insert (design 1A).
    audit = _make(db_url)
    try:
        with transaction(audit.engine) as conn:
            for row_id, act in [(1, "L1"), (2, "L2"), (4, "L4"), (5, "L5")]:  # rollback gap at id 3
                conn.execute(_audits.insert().values(id=row_id, action=act, created_at=now_utc()))
        audit.sealer.run_once()  # seq 1 covers (0, 5], row_count 4, boundary 5
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(id=3, action="FORGED", created_at=now_utc()))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
        assert report.warning_count == 0  # not an amber late-commit
    finally:
        audit.close()


def test_two_phase_rollout_straggler_without_key_is_not_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        # Phase 1: keyed writers are deployed, but one straggler still writes without the key.
        audit.record("keyed.a")
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="straggler", created_at=now_utc()))
        audit.record("keyed.b")

        # Phase 2: the first seal makes every pre-existing row part of the activation boundary.
        audit.sealer.run_once()
        report = audit.verify(full=True)
        assert report.outcome != "tampered"
        assert report.tampered_count == 0
        assert report.exit_code == 0
        assert report.unprotected_count >= 1
    finally:
        audit.close()


# -- tamper matrix (each → TAMPERED, exit non-zero) ---------------------------------------------


_EDIT_COLUMNS = {
    "action": "HACKED",
    "subject_type": "Widget",
    "subject_id": "999",
    "subject_label": "renamed",
    "actor_type": "Bot",
    "actor_id": "666",
    "actor_label": "renamed-actor",
    "correlation_id": "forged",
    "data": '{"amount": 999999}',
    "changes": '{"x": 1}',
    "context": '{"ip": "evil"}',
    "entry_id": "00000000000000000000000000",
}


@pytest.mark.parametrize("column,value", list(_EDIT_COLUMNS.items()), ids=list(_EDIT_COLUMNS))
def test_editing_any_column_is_tampered(db_url: str, column: str, value: str) -> None:
    audit = _make(db_url)
    try:
        audit.record(
            "obj.changed",
            subject=Ref("Invoice", 1, name="Acme"),
            actor=Ref("User", 2, name="alice"),
            data={"amount": 100},
            changes={"a": 1},
            context={"ip": "10.0.0.1"},
            correlation_id="req-1",
        )
        audit.sealer.run_once()
        target = _rows(audit.engine)[0]
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == target.id).values(**{column: value}))

        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
        assert report.tampered_count >= 1
    finally:
        audit.close()


def test_editing_created_at_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        target = _rows(audit.engine)[0]
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.id == target.id)
                .values(created_at=target.created_at + timedelta(seconds=1))
            )
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
    finally:
        audit.close()


def test_deleting_a_sealed_row_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        victim = _rows(audit.engine)[1].id
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_forged_insert_after_boundary_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()  # boundary now exists
        # A NULL-MAC row above the boundary: an instance without the key, or a forged insert.
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="forged", created_at=now_utc()))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
    finally:
        audit.close()


def test_forged_row_with_garbage_mac_in_sealed_range_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        # Count-preserving swap: delete a sealed row, insert a forged replacement at the same id
        # with a plausible-but-wrong row_mac. Count is preserved; rows_mac and row MAC both fail.
        victim = _rows(audit.engine)[1].id
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
            conn.execute(
                _audits.insert().values(
                    id=victim,
                    action="forged",
                    created_at=now_utc(),
                    entry_id="ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
                    row_mac="00" * 32,
                    key_id=_KEY.id,
                )
            )
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
    finally:
        audit.close()


def test_duplicate_entry_id_reported_when_index_bypassed(db_url: str, is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("dropping the unique index to simulate a bypass is SQLite-only here")
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        original = _rows(audit.engine)[0]
        # Simulate an attacker who dropped the unique index and replayed a real row.
        with transaction(audit.engine) as conn:
            conn.exec_driver_sql("DROP INDEX index_firm_audit_events_on_entry_id")
            conn.execute(
                _audits.insert().values(
                    action=original.action,
                    created_at=original.created_at,
                    entry_id=original.entry_id,
                    row_mac=original.row_mac,
                    key_id=original.key_id,
                )
            )
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert any("appears more than once" in f.message for f in report.findings)
    finally:
        audit.close()


def test_editing_a_seal_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.seq == 1).values(row_count=99))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert any("seal_mac" in f.message for f in report.findings)
    finally:
        audit.close()


def test_deleting_a_mid_chain_seal_is_tampered(db_url: str) -> None:
    audit = _make(db_url, seal_batch_size=1)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # three seals: seq 1, 2, 3
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.seq == 2))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert any("gap" in f.message for f in report.findings)
    finally:
        audit.close()


def test_reordering_seals_breaks_the_chain(db_url: str) -> None:
    audit = _make(db_url, seal_batch_size=1)
    try:
        for i in range(2):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # seq 1, 2
        seals = {s.seq: s for s in _seal_all(audit.engine)}
        # Swap the two seal_macs (as if the rows were reordered): each seal_mac no longer recomputes
        # and seq 2's prev_mac no longer links to seq 1.
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.seq == 1).values(seal_mac=seals[2].seal_mac))
            conn.execute(update(_seals).where(_seals.c.seq == 2).values(seal_mac=seals[1].seal_mac))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
    finally:
        audit.close()


def _seal_all(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.seq)).all()


# -- anchor: truncation & drop-recreate (Layer 3) -----------------------------------------------


def test_truncating_the_seal_tail_is_caught_by_the_anchor(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, seal_batch_size=3, anchor_path=str(anchor))
    try:
        for i in range(3):
            audit.record(f"a{i}")
        audit.sealer.run_once()  # seq 1
        for i in range(3):
            audit.record(f"b{i}")
        audit.sealer.run_once()  # seq 2, anchored
        # Truncate the tail: drop seq 2 and its rows. The chain now looks internally consistent...
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.seq == 2))
            conn.execute(delete(_audits).where(_audits.c.id > 3))
        # ...but the anchor still names seq 2, so verify catches the truncation.
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_drop_and_recreate_is_caught_by_the_anchor(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor))
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        # Wholesale reset: empty both tables (a "clean" but forged empty chain).
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals))
            conn.execute(delete(_audits))
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "tampered"
    finally:
        audit.close()


def test_stale_anchor_forces_non_zero_exit(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    # A tiny anchor_max_age makes the freshly written anchor already "stale".
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=0.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "warning"  # not tampered — the chain is fine
        assert report.exit_code == 1  # ...but a stale anchor still exits non-zero (D16)
    finally:
        audit.close()


def test_missing_anchor_file_with_seals_warns(db_url: str, tmp_path) -> None:
    missing = tmp_path / "nope.log"
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        report = audit.verify(anchor_path=str(missing), full=True)
        assert report.outcome == "warning"
        assert report.anchor_configured is True
        assert any("missing or empty" in f.message for f in report.findings)
    finally:
        audit.close()


def test_legacy_three_field_anchor_line_does_not_crash_verify(db_url: str, tmp_path) -> None:
    # A 3-field anchor line ("<sealed_at> <seq> <seal_mac>", from before the to_id column) or a
    # partial best-effort append must NOT raise out of verify and freeze the status — it is read
    # (legacy) or skipped (garbage), never crashed on.
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=3600.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        newest = anchor.read_text().splitlines()[-1].split()
        # Rewrite as a legacy 3-field line (drop the to_id column) + a garbage trailing line.
        anchor.write_text(f"{newest[0]} {newest[1]} {newest[3]}\nnot a valid line\n")
        report = audit.verify(anchor_path=str(anchor), full=True)  # must not raise
        assert report.outcome == "ok"  # legacy line still carries a valid seq/seal_mac
    finally:
        audit.close()


def test_malformed_retired_keyring_env_is_error_not_crash(db_url: str, monkeypatch) -> None:
    # A typo'd FIRM_AUDIT_RETIRED_KEYS surfaces as verify's error outcome (persisted), not an
    # uncaught ValueError that leaves the status frozen at its last verdict.
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", "no-equals-sign-here")
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with pytest.raises(VerifyError):
            audit.verify(full=True)
        assert _status_row(audit.engine).outcome == "error"  # persisted, not frozen
    finally:
        audit.close()


def test_valid_anchor_verifies_ok(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=3600.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.anchor_configured is True
        assert report.newest_anchor_at is not None
    finally:
        audit.close()


# -- verdict classes: late commit, unknown key, liveness ----------------------------------------


def test_late_commit_is_a_warning_not_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("first")
        audit.record("late")  # id 2 — modelled as committing after the range was sealed
        audit.record("third")
        rows = _rows(audit.engine)
        # A seal that covers ids (0, 3] but only counted rows 1 and 3 (row 2 "committed late").
        pairs = [(rows[0].id, rows[0].row_mac), (rows[2].id, rows[2].row_mac)]
        _insert_manual_seal(
            audit.engine,
            seq=1,
            from_id=0,
            to_id=rows[2].id,
            pairs=pairs,
            row_count=2,
            prev_mac="genesis",
        )
        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert report.exit_code == 0
        assert report.tampered_count == 0
        assert any("late" in f.message for f in report.findings)
    finally:
        audit.close()


def test_delete_and_relocate_into_gaps_is_tampered_not_late_commit(db_url: str) -> None:
    # Bug #1 laundering attack. A DB-write attacker with NO key deletes a genuinely-sealed row and
    # back-fills id-gaps in the range with other valid signed rows (relocated — changing ``id``,
    # which the row MAC used to ignore) so the present count climbs PAST ``row_count``. Before the
    # fix that read as a benign "late_commit" WARNING (every present row is validly signed and there
    # are more than were sealed), which retention would then prune, laundering the deletion. The
    # seal now signs its covered membership (``gap_ranges``), so the deleted covered row is caught:
    # the covered subset no longer reproduces ``rows_mac`` → TAMPERED.
    audit = _make(db_url)
    try:
        for a in ("a", "b", "c", "d", "e", "f"):
            audit.record(a)  # ids 1..6
        rows = _rows(audit.engine)
        gap_id = rows[2].id  # delete the middle row BEFORE sealing → a recorded gap at its id
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == gap_id))
        audit.sealer.run_once()  # seals {1,2,4,5,6}, gap {3}, row_count 5, to_id 6
        (seal,) = [s for s in _seal_rows(audit.engine) if s.kind == "seal"]
        assert seal.row_count == 5
        assert audit.verify(full=True).outcome == "ok"

        # Two valid signed rows to relocate (recorded as the unsealed tail, ids 7 & 8).
        audit.record("src1")
        audit.record("src2")
        tail = [r for r in _rows(audit.engine) if r.id > seal.to_id]
        victim = rows[4].id  # a genuinely-sealed row (id 5) whose evidence the attacker erases

        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
            # Relocate the two valid tail rows into the freed slots inside the sealed range: one
            # into the recorded gap (id 3), one onto the deleted covered id (id 5). Count → 6 > 5.
            conn.execute(update(_audits).where(_audits.c.id == tail[0].id).values(id=gap_id))
            conn.execute(update(_audits).where(_audits.c.id == tail[1].id).values(id=victim))

        present = [r.id for r in _rows(audit.engine) if seal.from_id < r.id <= seal.to_id]
        assert len(present) == 6  # 6 present rows in a range that sealed 5 — the late-commit shape
        report = audit.verify(full=True)
        assert report.outcome == "tampered"  # NOT "warning"/late_commit — the deletion is caught
        assert report.exit_code == 1
    finally:
        audit.close()


def _seal_rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.seq)).all()


def test_unknown_key_id_is_a_hard_error(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(key_id="ffffffff"))
        with pytest.raises(VerifyError, match="unknown key_id"):
            audit.verify(full=True)
        # The error outcome is persisted before the exception re-raises (D24).
        status = _status_row(audit.engine)
        assert status.outcome == "error"
        assert "unknown key_id" in status.error_message
    finally:
        audit.close()


def test_rotation_key_in_keyring_verifies_ok(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Single-key rotation: the old key signed both the rows AND the seal, so it retires into
    # FIRM_AUDIT_RETIRED_SEAL_KEYS (the higher-privilege archive, eligible for rows *and* seals).
    # Verify runs under a *different* current key; the old key's key_id resolves and everything
    # verifies. (Retiring it into FIRM_AUDIT_RETIRED_KEYS instead would leave the seal it signed
    # unverifiable — that is the deliberate role split; see the two-key suite.)
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
    finally:
        audit.close()

    other = "another-secret-key-padding-0123456789ab"
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old={_SECRET}")
    verifier = AuditLog(database_url=db_url, mac_key=other, create_schema=False)
    try:
        report = verifier.verify(full=True)
        assert report.outcome == "ok"
    finally:
        verifier.close()


def _signed_row_values(action: str, created_at: datetime) -> dict:
    """A raw insert dict for a *validly signed* row at a chosen ``created_at`` — lets a test place a
    genuine unsealed row in the past without editing (and thereby invalidating) its MAC."""
    entry_id = new_ulid(created_at)
    mac = row_mac(
        _KEY,
        entry_id=entry_id,
        action=action,
        subject_type=None,
        subject_id=None,
        subject_label=None,
        actor_type=None,
        actor_id=None,
        actor_label=None,
        correlation_id=None,
        data=None,
        changes=None,
        context=None,
        created_at=created_at,
    )
    return {
        "action": action,
        "created_at": created_at,
        "entry_id": entry_id,
        "row_mac": mac,
        "key_id": _KEY.id,
    }


def test_stalled_sealer_unsealed_tail_age_warns(db_url: str) -> None:
    audit = _make(db_url, unsealed_tail_max_age=1.0)
    try:
        audit.record("sealed")
        audit.sealer.run_once()  # a seal exists → sealing is "active"
        # A genuine, validly signed unsealed row that is an hour old: the sealer has fallen behind.
        # It is authentic, so the only verdict is the sealer-liveness WARNING (exit 0), not tamper.
        with transaction(audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    **_signed_row_values("stuck", now_utc() - timedelta(hours=1))
                )
            )
        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert report.exit_code == 0
        assert any("stalled" in f.message for f in report.findings)
    finally:
        audit.close()


# -- rolling coverage & advisory state (D12) ----------------------------------------------------


def test_rolling_coverage_detects_old_range_edit_within_cycle(db_url: str) -> None:
    # Six single-row seals, verify_cycle=3 → each default (non-full) run recomputes ~2 old ranges
    # plus the newest; a full sweep completes within 3 runs.
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3)
    try:
        for i in range(6):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        # Tamper with the oldest range's row.
        oldest = _rows(audit.engine)[0].id
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == oldest).values(action="HACKED"))
        outcomes = [audit.verify().outcome for _ in range(3)]  # non-full, rolling
        assert "tampered" in outcomes
    finally:
        audit.close()


def test_rolling_coverage_advances_cursor_to_reach_a_middle_range(db_url: str, tmp_path) -> None:
    # The sibling to the test above: that one tampers range index 0, which the first non-full run
    # always recomputes (newest + the first rotating slice), so it proves detection but not that the
    # rotation cursor *advances*. Here the tampered range is a middle one the first run does not
    # recompute, and the state cursor is persisted (as a per-run cron would), so catching it later
    # can only happen if the cursor moved forward across runs (D12).
    state = tmp_path / "verify.state"  # fresh (absent) -> cursor starts at 0
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3, verify_state_path=str(state))
    try:
        for i in range(6):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # 6 covering seals (indices 0..5); per_run = ceil(6/3) = 2
        # Run 1 recomputes indices {0, 1} + the always-checked newest {5}; index 3 is a middle range
        # it skips. Rotation must reach it on a later run for the edit to surface.
        victim = _rows(audit.engine)[3].id
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == victim).values(action="HACKED"))
        outcomes = [audit.verify().outcome for _ in range(3)]  # non-full, rolling; <= verify_cycle
        assert outcomes[0] != "tampered"  # the middle range is not recomputed on run 1
        assert "tampered" in outcomes[1:]  # ...but the advancing cursor reaches it within the cycle
    finally:
        audit.close()


def test_rolling_coverage_without_state_path_warns(db_url: str) -> None:
    # With no persisted rotation state (the default), a fresh per-run `firm-audit verify` process
    # keeps its cursor only in memory and never rotates to older ranges (D12). That silent coverage
    # gap must be surfaced as a warning rather than a green run that only swept the newest ranges.
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3)
    try:
        for i in range(6):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # 6 covering seals; per_run = 2 < 6, so rotation actually matters
        with pytest.warns(UserWarning, match="persisted rotation state"):
            audit.verify()  # non-full
    finally:
        audit.close()


def test_corrupted_state_file_cannot_suppress_detection(db_url: str, tmp_path) -> None:
    # An attacker with host access rewrites the advisory rotation cursor to a *plausible* value
    # (not garbage) chosen to skip the tampered range on the first run. Because the state is
    # advisory (D12) — rotation still sweeps every range within verify_cycle *non-full* runs — the
    # edit surfaces regardless. This exercises the rolling path itself (not --full, which would
    # ignore the cursor and recompute everything, proving nothing about suppression).
    state = tmp_path / "verify.state"
    state.write_text("2", encoding="utf-8")  # plausible cursor, seeded to skip the tampered range
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3, verify_state_path=str(state))
    try:
        for i in range(4):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # 4 covering seals (indices 0..3); per_run = ceil(4/3) = 2
        # Tamper a middle range the cursor skips on run 1 (index 1); rotation reaches it next.
        victim = _rows(audit.engine)[1].id
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == victim).values(action="HACKED"))
        outcomes = [audit.verify().outcome for _ in range(3)]  # non-full, rolling
        assert "tampered" in outcomes
    finally:
        audit.close()


def test_pinned_cursor_defers_a_range_from_non_full_runs_but_full_catches_it(
    db_url: str, tmp_path
) -> None:
    # Adversarial finding (MEDIUM): the rotation cursor is not MAC-protected, so an attacker who
    # *pins* it — rewriting it to the same value before every non-``--full`` run — keeps one chosen
    # range out of the rolling slice indefinitely. This codifies the honest contract the docstring
    # now states: only ``--full`` guarantees coverage of every sealed range; the rolling cursor is a
    # freshness optimization, not a security guarantee against a cursor-tampering attacker.
    state = tmp_path / "verify.state"
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3, verify_state_path=str(state))
    try:
        for i in range(4):
            audit.record(f"e{i}")
        audit.sealer.run_once()  # 4 covering seals (indices 0..3); per_run = ceil(4/3) = 2
        # per_run=2 with cursor pinned to 0 selects covering indices {0, 1} plus the newest {3};
        # index 2 is the range the pin permanently skips. seal_batch_size=1 → seal k covers row k,
        # so row index 2 lives in covering[2].
        victim = _rows(audit.engine)[2].id
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == victim).values(action="HACKED"))
        # Each non-full run is a fresh per-run cron process that reloads the cursor from the
        # (pinned) state file. The attacker re-pins it to 0 before every run, so the tampered middle
        # range is never in the rolling slice and rolling verify keeps returning non-tampered.
        pinned = []
        for _ in range(5):
            state.write_text("0", encoding="utf-8")
            run = _make(
                db_url,
                seal_batch_size=1,
                verify_cycle=3,
                verify_state_path=str(state),
                create_schema=False,
            )
            try:
                pinned.append(run.verify().outcome)
            finally:
                run.close()
        assert "tampered" not in pinned  # the pin defers detection on the rolling path
        # ...but --full ignores the cursor and recomputes every range, so it catches it.
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


# -- status row upsert --------------------------------------------------------------------------


def test_verify_upserts_single_status_row(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.verify(full=True)
        audit.verify(full=True)  # a second run must upsert, not append
        with transaction(audit.engine) as conn:
            count = conn.execute(select(func.count()).select_from(_status)).scalar_one()
        assert count == 1
        status = _status_row(audit.engine)
        assert status.outcome == "ok"
        assert status.ran_at is not None
        assert status.anchor_configured is False
        assert status.last_full_coverage_at is not None
        assert status.cycle_length == 7
    finally:
        audit.close()


def test_status_row_records_affected_identifiers_on_tampering(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        audit.verify(full=True)
        status = _status_row(audit.engine)
        assert status.outcome == "tampered"
        assert status.tampered_count >= 1
        # affected_identifiers is a JSON list of structured, linkable findings — not a flat label.
        items = json.loads(status.affected_identifiers)
        assert isinstance(items, list) and items
        finding = next(i for i in items if i["kind"] == "row")
        assert finding["id"] == 1  # links the chip into /audit/1
        assert finding["label"].startswith("#1 ")  # "#<id> <action>", a meaningful identity
        assert finding["verdict"] == "tampered"
        assert "modified after it was sealed" in finding["message"]  # plain-language what/why
    finally:
        audit.close()


def test_malformed_gap_ranges_is_tampered_not_a_crash(db_url: str) -> None:
    # Bug C (HIGH): a seal whose ``gap_ranges`` is unparseable made ``integrity.parse_gaps`` raise
    # ValueError, which propagated out of the whole verify run — the run never persisted a status,
    # so the dashboard's last row stayed frozen at OK while the database was tampered. verify must
    # instead classify such a seal TAMPERED and persist it, never raise-and-freeze.
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.verify(full=True)  # seeds a clean "ok" status row first
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).values(row_count=99, gap_ranges="not-an-interval"))
        # verify returns a verdict rather than raising the ValueError out of the run …
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        # … and the frozen "ok" status row was overwritten with the tampered outcome.
        assert _status_row(audit.engine).outcome == "tampered"
    finally:
        audit.close()


def test_mass_tamper_keeps_findings_bounded_but_persists_tampered(db_url: str) -> None:
    # Bug #6. Verify used to append one Finding per tampered row, capping only at serialize time — a
    # mass tamper could grow the in-memory list without bound and OOM verify BEFORE it persisted the
    # red status or fired the alert. Findings are now capped during accumulation while the counters
    # stay exact, so the run always lands the TAMPERED outcome with the true count, and alerts.
    from firm.audit import verify as verify_mod

    n = verify_mod._MAX_FINDINGS + 50
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        for i in range(n):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        # Tamper EVERY sealed row (each recomputes to a different MAC → per-row tampered).
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        report = audit.verify(full=True)

        # The in-memory findings list is bounded, but the counts are exact and persisted.
        assert len(report.findings) <= verify_mod._MAX_FINDINGS
        assert report.outcome == "tampered"
        assert report.tampered_count == n  # exact, from the counters — not the capped list
        status = _status_row(audit.engine)
        assert status.outcome == "tampered"
        assert status.tampered_count == n
        # The alert still fired, with the exact count and a bounded sample of identifiers.
        assert len(alerts) == 1
        assert alerts[0].tampered_count == n
        assert len(alerts[0].affected) <= 20
        # The persisted affected_identifiers stays small, and its overflow marker is honest.
        items = json.loads(status.affected_identifiers)
        assert len(items) <= 21  # 20 findings + one "+N more" marker
        more = next(i for i in items if i["kind"] == "more")
        assert f"+{n - 20} more" in more["label"]
    finally:
        audit.close()


# -- on_finding: high-severity alert on detection ----------------------------------------------


def test_on_finding_fires_critical_alert_on_tampering(db_url: str) -> None:
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        audit.verify(full=True)
    finally:
        audit.close()
    assert len(alerts) == 1  # once per run
    alert = alerts[0]
    assert alert.severity == "critical"
    assert alert.outcome == "tampered"
    assert alert.tampered_count >= 1
    assert any(a.startswith("#1 ") for a in alert.affected)


def test_on_finding_silent_on_ok_verify(db_url: str) -> None:
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.verify(full=True)
    finally:
        audit.close()
    assert alerts == []  # ok/unprotected never alert


def test_on_finding_fires_warning_severity_on_late_commit(db_url: str) -> None:
    # A valid-MAC row in an already-sealed range is a WARNING (late commit), not tampering.
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        audit.record("a")
        audit.record("b")
        # Hand-build a seal that spans both rows but claims only one — every present row is validly
        # signed, so the surplus row is a valid-MAC late commit (WARNING), not tampering.
        rows = _rows(audit.engine)
        _insert_manual_seal(
            audit.engine,
            seq=1,
            from_id=0,
            to_id=rows[-1].id,
            pairs=[(rows[0].id, rows[0].row_mac)],
            row_count=1,
            prev_mac="genesis",
        )
        report = audit.verify(full=True)
    finally:
        audit.close()
    assert report.outcome == "warning"
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert alerts[0].warning_count >= 1


def test_default_sink_writes_one_stderr_line_on_tampering(db_url: str, capsys) -> None:
    audit = _make(db_url)  # no on_finding -> the default stderr sink
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        capsys.readouterr()  # drop the sealing-enabled warning noise
        audit.verify(full=True)
    finally:
        audit.close()
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("firm-audit:")]
    assert len(lines) == 1  # exactly one high-severity line
    assert "CRITICAL tamper detected" in lines[0]
    assert "#1 " in lines[0]


def test_failing_on_finding_callback_routes_to_on_error(db_url: str) -> None:
    errors: list[BaseException] = []

    def boom(_alert: IntegrityAlert) -> None:
        raise RuntimeError("sink is down")

    audit = _make(db_url, on_finding=boom, on_error=errors.append)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        # A broken sink must not crash the (read-only) verify.
        report = audit.verify(full=True)
    finally:
        audit.close()
    assert report.outcome == "tampered"
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


# -- CLI exit codes -----------------------------------------------------------------------------


def test_cli_verify_exit_codes_and_seal(db_url: str, is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("CLI test drives a file URL; run it on the SQLite backend")
    from click.testing import CliRunner

    from firm.audit.cli import main

    # Seed and seal a clean log via the API (grace 0), so we don't have to edit created_at (which
    # would itself invalidate the MAC).
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.record("b")
        audit.sealer.run_once()
    finally:
        audit.close()

    runner = CliRunner()
    env = {"FIRM_AUDIT_KEY": _SECRET}

    # `seal` runs cleanly with nothing new to seal (the backlog is already drained).
    sealed = runner.invoke(main, ["seal", "--database-url", db_url], env=env)
    assert sealed.exit_code == 0
    assert "sealed" in sealed.output

    ok = runner.invoke(main, ["verify", "--database-url", db_url, "--full"], env=env)
    assert ok.exit_code == 0
    assert "OK" in ok.output

    # Now tamper and confirm a non-zero exit.
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, create_schema=False)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.action == "a").values(action="HACKED"))
    finally:
        audit.close()

    bad = runner.invoke(main, ["verify", "--database-url", db_url, "--full"], env=env)
    assert bad.exit_code == 1
    assert "TAMPERED" in bad.output
