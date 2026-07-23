"""Verification of rows, independent seals, markers, anchor, rotation, and status persistence."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, Ref, schema
from firm.audit.integrity import load_key, new_ulid, row_mac, rows_mac, seal_mac
from firm.audit.verify import IntegrityAlert, VerifyError

_SECRET = "verify-secret-key-padding-0123456789ab"  # noqa: S105
_KEY = load_key(_SECRET)
assert _KEY is not None
_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FIRM_AUDIT_KEY",
        "FIRM_AUDIT_SEAL_KEY",
        "FIRM_AUDIT_RETIRED_KEYS",
        "FIRM_AUDIT_RETIRED_SEAL_KEYS",
        "FIRM_AUDIT_ANCHOR_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _make(db_url: str, *, activate: bool = True, **kwargs) -> AuditLog:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, **kwargs)
    if activate:
        assert audit.sealer.run_once() == 0
    return audit


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _records(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.id)).all()


def _range_seals(engine) -> list:
    return [record for record in _records(engine) if record.kind == "seal"]


def _status_row(engine):
    with transaction(engine) as conn:
        return conn.execute(select(_status)).first()


def _insert_manual_seal(audit: AuditLog, pairs: list[tuple[int, str]], *, to_id: int) -> None:
    at = now_utc()
    aggregate = rows_mac(_KEY, pairs)
    mac = seal_mac(
        _KEY,
        from_id=0,
        to_id=to_id,
        row_count=len(pairs),
        rows_mac=aggregate,
        sealed_at=at,
        key_id=_KEY.id,
    )
    with transaction(audit.engine) as conn:
        conn.execute(
            _seals.insert().values(
                kind="seal",
                from_id=0,
                to_id=to_id,
                row_count=len(pairs),
                rows_mac=aggregate,
                seal_mac=mac,
                sealed_at=at,
                key_id=_KEY.id,
            )
        )


def test_empty_log_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.unsealed_tail_count == 0
    finally:
        audit.close()


def test_single_row_sealed_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("only")
        assert audit.sealer.run_once() == 1
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.ok_count == 1
    finally:
        audit.close()


def test_all_sealed_is_ok(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for index in range(5):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.ok_count == 5
        assert report.unsealed_tail_count == 0
    finally:
        audit.close()


def test_unsealed_tail_rows_are_ok_and_counted(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.record("b")
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unsealed_tail_count == 1
    finally:
        audit.close()


def test_legacy_only_log_is_unprotected_not_tampered(db_url: str) -> None:
    audit = _make(db_url, activate=False)
    try:
        with transaction(audit.engine) as conn:
            for action in ("x", "y", "z"):
                conn.execute(_audits.insert().values(action=action, created_at=now_utc()))
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unprotected_count == 3
    finally:
        audit.close()


def test_activation_excludes_legacy_backlog_from_sealing(db_url: str) -> None:
    audit = _make(db_url, activate=False, seal_batch_size=2)
    try:
        with transaction(audit.engine) as conn:
            for index in range(5):
                conn.execute(_audits.insert().values(action=f"legacy{index}", created_at=now_utc()))
        assert audit.sealer.run_once() == 0
        assert _range_seals(audit.engine) == []
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unprotected_count == 5
    finally:
        audit.close()


def test_null_mac_row_above_activation_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="FORGED", created_at=now_utc()))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.warning_count == 0
    finally:
        audit.close()


def test_interleaved_pre_activation_straggler_leaves_only_earlier_keyed_row_unsealed(
    db_url: str,
) -> None:
    audit = _make(db_url, activate=False)
    try:
        audit.record("keyed.a")
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="straggler", created_at=now_utc()))
        audit.record("keyed.b")
        assert audit.sealer.run_once() == 1
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unprotected_count == 1
    finally:
        audit.close()


_EDIT_COLUMNS = {
    "action": "HACKED",
    "subject_type": "Widget",
    "subject_id": "999",
    "subject_label": "renamed",
    "actor_type": "Bot",
    "actor_id": "666",
    "actor_label": "renamed-actor",
    "correlation_id": "forged",
    "data": '{"amount":999999}',
    "changes": '{"x":1}',
    "context": '{"ip":"evil"}',
    "entry_id": "00000000000000000000000000",
}


@pytest.mark.parametrize("column,value", _EDIT_COLUMNS.items(), ids=_EDIT_COLUMNS)
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
        assert audit.verify(full=True).outcome == "tampered"
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
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_deleting_a_sealed_row_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == _rows(audit.engine)[1].id))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_forged_insert_after_boundary_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="forged", created_at=now_utc()))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_forged_row_with_garbage_mac_in_sealed_range_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        victim = _rows(audit.engine)[1].id
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
            conn.execute(
                _audits.insert().values(
                    id=victim,
                    action="forged",
                    created_at=now_utc(),
                    entry_id="ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
                    row_mac="0" * 64,
                    key_id=_KEY.id,
                )
            )
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_duplicate_entry_id_reported_when_index_bypassed(db_url: str, is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("SQLite-only index bypass")
    audit = _make(db_url)
    try:
        audit.record("a")
        original = _rows(audit.engine)[0]
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
        assert any("appears more than once" in finding.message for finding in report.findings)
    finally:
        audit.close()


def test_editing_a_seal_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.kind == "seal").values(row_count=99))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_deleting_a_middle_independent_seal_breaks_contiguity(db_url: str) -> None:
    audit = _make(db_url, seal_batch_size=1)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        middle = _range_seals(audit.engine)[1]
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.id == middle.id))
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert any("not contiguous" in finding.message for finding in report.findings)
    finally:
        audit.close()


def test_swapping_seal_coordinates_breaks_independent_macs(db_url: str) -> None:
    audit = _make(db_url, seal_batch_size=1)
    try:
        audit.record("a")
        audit.record("b")
        audit.sealer.run_once()
        first, second = _range_seals(audit.engine)
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.id == first.id).values(to_id=second.to_id))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_truncating_the_seal_tail_is_caught_by_anchor(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=0.0, seal_batch_size=1)
    try:
        audit.record("a")
        audit.record("b")
        audit.sealer.run_once()
        tail = _range_seals(audit.engine)[-1]
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id > tail.from_id))
            conn.execute(delete(_seals).where(_seals.c.id == tail.id))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_drop_and_recreate_is_caught_by_anchor(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor))
    try:
        audit.record("a")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals))
            conn.execute(delete(_audits))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_stale_anchor_forces_non_zero_exit(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=0.0)
    try:
        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_missing_anchor_file_with_records_warns(db_url: str, tmp_path) -> None:
    audit = _make(db_url)
    try:
        report = audit.verify(anchor_path=str(tmp_path / "missing.log"), full=True)
        assert report.outcome == "warning"
        assert report.anchor_configured is True
    finally:
        audit.close()


def test_malformed_final_anchor_line_warns_not_crash(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor))
    try:
        line = anchor.read_text().splitlines()[0].split()
        anchor.write_text(f"{line[0]} 1 {line[-1]}\n", encoding="utf-8")
        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert _status_row(audit.engine).outcome == "warning"
    finally:
        audit.close()


def test_malformed_retired_keyring_env_is_error_not_crash(db_url: str, monkeypatch) -> None:
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", "no-equals")
    audit = _make(db_url)
    try:
        with pytest.raises(VerifyError):
            audit.verify(full=True)
        assert _status_row(audit.engine).outcome == "error"
    finally:
        audit.close()


def test_valid_anchor_verifies_ok(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = _make(db_url, anchor_path=str(anchor), anchor_max_age=3600.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.newest_anchor_at is not None
    finally:
        audit.close()


def test_surplus_valid_row_is_tampered_without_leniency(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("first")
        audit.record("late")
        rows = _rows(audit.engine)
        _insert_manual_seal(audit, [(rows[0].id, rows[0].row_mac)], to_id=rows[-1].id)
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.warning_count == 0
    finally:
        audit.close()


def test_delete_and_relocate_is_tampered(db_url: str) -> None:
    audit = _make(db_url)
    try:
        for action in ("a", "b", "c"):
            audit.record(action)
        audit.sealer.run_once()
        rows = _rows(audit.engine)
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == rows[1].id))
            conn.execute(update(_audits).where(_audits.c.id == rows[2].id).values(id=rows[1].id))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_unknown_key_id_is_a_hard_error(db_url: str) -> None:
    errors: list[BaseException] = []
    audit = _make(db_url, on_error=errors.append)
    try:
        audit.record("a")
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(key_id="ffffffff"))
        with pytest.raises(VerifyError, match="unknown key_id"):
            audit.verify(full=True)
        assert _status_row(audit.engine).outcome == "error"
        assert errors and isinstance(errors[0], VerifyError)
    finally:
        audit.close()


def test_rotation_key_in_keyring_verifies_ok(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
    finally:
        audit.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old={_SECRET}")
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old={_SECRET}")
    verifier = AuditLog(
        database_url=db_url,
        mac_key="another-secret-key-padding-0123456789ab",
        create_schema=False,
    )
    try:
        assert verifier.verify(full=True).outcome == "ok"
    finally:
        verifier.close()


def _signed_row_values(action: str, created_at: datetime) -> dict:
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
        with transaction(audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    **_signed_row_values("stuck", now_utc() - timedelta(hours=1))
                )
            )
        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert report.exit_code == 0
    finally:
        audit.close()


def test_stateless_date_slice_reaches_old_ranges(db_url: str, monkeypatch) -> None:
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3)
    try:
        for index in range(6):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        victim = _rows(audit.engine)[2]
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == victim.id).values(action="HACKED"))
        base = datetime(2026, 7, 20)
        outcomes = []
        for offset in range(6):
            monkeypatch.setattr(
                "firm.audit.verify.now_utc", lambda offset=offset: base + timedelta(days=offset)
            )
            outcomes.append(audit.verify().outcome)
        assert "tampered" in outcomes
    finally:
        audit.close()


def test_stateless_slice_does_not_create_or_read_cursor_file(db_url: str, tmp_path) -> None:
    state = tmp_path / "verify.state"
    state.write_text("attacker-controlled", encoding="utf-8")
    audit = _make(db_url, seal_batch_size=1, verify_cycle=3)
    try:
        for index in range(4):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        audit.verify()
        assert state.read_text(encoding="utf-8") == "attacker-controlled"
    finally:
        audit.close()


def test_verify_state_path_constructor_option_is_removed(db_url: str, tmp_path) -> None:
    with pytest.raises(TypeError, match="verify_state_path"):
        AuditLog(database_url=db_url, mac_key=_SECRET, verify_state_path=str(tmp_path / "state"))


def test_full_catches_range_outside_current_date_slice(db_url: str) -> None:
    audit = _make(db_url, seal_batch_size=1, verify_cycle=100)
    try:
        for index in range(5):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        selected = audit.verifier._select_ranges(
            _range_seals(audit.engine), full=False, now=now_utc()
        )
        victim = next(record for record in _range_seals(audit.engine) if record not in selected)
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_audits).where(_audits.c.id == victim.to_id).values(action="HACKED")
            )
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_verify_upserts_single_status_row_without_cycle_columns(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.verify(full=True)
        audit.verify(full=True)
        with transaction(audit.engine) as conn:
            assert conn.execute(select(func.count()).select_from(_status)).scalar_one() == 1
        status = _status_row(audit.engine)
        assert status.outcome == "ok"
        assert status.last_full_coverage_at is not None
        assert not hasattr(status, "cycle_length")
        assert not hasattr(status, "cycle_position")
    finally:
        audit.close()


def test_broken_status_sink_does_not_mask_tampered_report(db_url: str) -> None:
    errors: list[BaseException] = []
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_error=errors.append, on_finding=alerts.append)
    try:
        with transaction(audit.engine) as conn:
            conn.exec_driver_sql("DROP TABLE firm_audit_verify_status")
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert errors
        assert alerts and alerts[0].severity == "critical"
    finally:
        audit.close()


def test_anchor_is_read_after_snapshot_opens(db_url: str, tmp_path, monkeypatch) -> None:
    from firm.audit import verify as verify_mod

    audit = _make(db_url)
    anchor = tmp_path / "anchor.log"
    order: list[str] = []
    original_snapshot = verify_mod.snapshot_transaction
    original_read_anchor = verify_mod._read_anchor

    @contextmanager
    def tracked_snapshot(engine):
        order.append("snapshot")
        with original_snapshot(engine) as conn:
            yield conn

    def tracked_read_anchor(path: str, **kwargs):
        order.append("anchor")
        return original_read_anchor(path, **kwargs)

    monkeypatch.setattr(verify_mod, "snapshot_transaction", tracked_snapshot)
    monkeypatch.setattr(verify_mod, "_read_anchor", tracked_read_anchor)
    try:
        audit.verify(anchor_path=str(anchor), full=True)
        assert order[:2] == ["snapshot", "anchor"]
    finally:
        audit.close()


def test_seal_side_table_scan_uses_keyset_pages(db_url: str, monkeypatch) -> None:
    from firm.audit import verify as verify_mod

    audit = _make(db_url, seal_batch_size=1)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        monkeypatch.setattr(verify_mod, "_PAGE", 1)
        assert audit.verify(full=True).outcome == "ok"
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
        items = json.loads(_status_row(audit.engine).affected_identifiers)
        finding = next(item for item in items if item["kind"] == "row")
        assert finding["id"] == 1
        assert finding["verdict"] == "tampered"
    finally:
        audit.close()


def test_malformed_seal_field_is_tampered_not_crash(db_url: str) -> None:
    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        audit.verify(full=True)
        with transaction(audit.engine) as conn:
            conn.exec_driver_sql(
                "UPDATE firm_audit_seals SET sealed_at='not-a-date' WHERE kind='seal'"
            )
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert _status_row(audit.engine).outcome == "tampered"
    finally:
        audit.close()


def test_mass_tamper_keeps_findings_bounded_but_persists_tampered(db_url: str) -> None:
    from firm.audit import verify as verify_mod

    count = verify_mod._MAX_FINDINGS + 50
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        for index in range(count):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        report = audit.verify(full=True)
        assert len(report.findings) <= verify_mod._MAX_FINDINGS
        assert report.tampered_count == count + 1  # rows plus the range mismatch
        assert _status_row(audit.engine).outcome == "tampered"
        assert alerts[0].tampered_count == count + 1
    finally:
        audit.close()


def test_unknown_row_tracking_is_bounded_and_reports_overflow(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from firm.audit import verify as verify_mod

    monkeypatch.setattr(verify_mod, "_MAX_FINDINGS", 3)
    audit = _make(db_url)
    try:
        for index in range(5):
            audit.record(f"unknown-{index}")
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(key_id="deadbeef"))
        with pytest.raises(VerifyError, match=r"\(\+2 more unresolved rows\)"):
            audit.verify(full=True)
    finally:
        audit.close()


def test_oversized_attacker_cell_is_tampered_without_unbounded_recompute(db_url: str) -> None:
    audit = _make(db_url)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    action="x" * 2_000_000,
                    created_at=now_utc(),
                    entry_id=new_ulid(),
                    row_mac="0" * 64,
                    key_id=_KEY.id,
                )
            )
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_memory_error_becomes_tampered_report(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _make(db_url)
    monkeypatch.setattr(
        "firm.audit.verify.load_seal_records",
        lambda _conn: (_ for _ in ()).throw(MemoryError("attacker-sized value")),
    )
    try:
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_on_finding_fires_critical_alert_on_tampering(db_url: str) -> None:
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="FORGED", created_at=now_utc()))
        audit.verify(full=True)
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
    finally:
        audit.close()


def test_on_finding_silent_on_ok_verify(db_url: str) -> None:
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append)
    try:
        audit.verify(full=True)
        assert alerts == []
    finally:
        audit.close()


def test_on_finding_fires_warning_severity_on_stale_tail(db_url: str) -> None:
    alerts: list[IntegrityAlert] = []
    audit = _make(db_url, on_finding=alerts.append, unsealed_tail_max_age=1.0)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    **_signed_row_values("stuck", now_utc() - timedelta(hours=1))
                )
            )
        audit.verify(full=True)
        assert alerts[0].severity == "warning"
    finally:
        audit.close()


def test_default_sink_writes_one_stderr_line_on_tampering(db_url: str, capsys) -> None:
    audit = _make(db_url)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="FORGED", created_at=now_utc()))
        capsys.readouterr()
        audit.verify(full=True)
        lines = [
            line for line in capsys.readouterr().err.splitlines() if line.startswith("firm-audit:")
        ]
        assert len(lines) == 1
        assert "CRITICAL tamper detected" in lines[0]
    finally:
        audit.close()


def test_failing_on_finding_callback_routes_to_on_error(db_url: str) -> None:
    errors: list[BaseException] = []

    def boom(_alert: IntegrityAlert) -> None:
        raise RuntimeError("sink is down")

    audit = _make(db_url, on_finding=boom, on_error=errors.append)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="FORGED", created_at=now_utc()))
        assert audit.verify(full=True).outcome == "tampered"
        assert len(errors) == 1
    finally:
        audit.close()


def test_cli_verify_exit_codes_and_seal(db_url: str, is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("CLI test uses the SQLite file URL")
    from click.testing import CliRunner

    from firm.audit.cli import main

    audit = _make(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
    finally:
        audit.close()
    runner = CliRunner()
    env = {"FIRM_AUDIT_KEY": _SECRET}
    assert (
        runner.invoke(main, ["verify", "--database-url", db_url, "--full"], env=env).exit_code == 0
    )
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, create_schema=False)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
    finally:
        audit.close()
    bad = runner.invoke(main, ["verify", "--database-url", db_url, "--full"], env=env)
    assert bad.exit_code == 1
    assert "TAMPERED" in bad.output
