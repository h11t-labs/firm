"""Plain and seal-aligned retention with append-only retirement floors."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema
from firm.audit.retention import RetentionLoop

_SECRET = "retention-secret-key-padding-0123456789"  # noqa: S105
_SEAL_SECRET = "retention-seal-key-padding-012345678"  # noqa: S105
_audits = schema.audit_events
_seals = schema.seals


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _records(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.id)).all()


def _activate(audit: AuditLog) -> None:
    assert audit.sealer.run_once() == 0


def _seal_old(audit: AuditLog, at_time, *actions: str) -> None:
    old = now_utc() - timedelta(hours=2)
    with at_time(old):
        for action in actions:
            audit.record(action)
    with patch("firm.audit.sealing.now_utc", lambda: old):
        assert audit.sealer.run_once() == len(actions)


def test_keep_forever_default_is_a_noop(audit: AuditLog) -> None:
    audit.record("a")
    assert audit.retention.run_once() == 0
    assert len(audit.history()) == 1


def test_plain_prune_deletes_only_old_rows(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        audit.record("new")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.action == "old")
                .values(created_at=now_utc() - timedelta(hours=2))
            )
        assert audit.retention.run_once() == 1
        assert [row["action"] for row in audit.history()] == ["new"]
    finally:
        audit.close()


def test_recording_never_triggers_retention(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=0.001)
    try:
        audit.record("a")
        time.sleep(0.02)
        audit.record("b")
        assert len(audit.history()) == 2
    finally:
        audit.close()


def test_background_retention_flag_starts_loop(db_url: str) -> None:
    audit = AuditLog(
        database_url=db_url, max_age=3600.0, background_retention=True, retention_interval=0.05
    )
    try:
        assert audit._loop is not None and audit._loop.name == "audit-retention"
    finally:
        audit.close()


def test_retention_loop_runs_a_pass(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(created_at=now_utc() - timedelta(hours=2)))
        loop = RetentionLoop(audit.retention, interval=0.01)
        loop.start()
        try:
            for _ in range(100):
                if not audit.history():
                    break
                time.sleep(0.01)
            assert audit.history() == []
        finally:
            loop.stop()
    finally:
        audit.close()


def test_background_retention_failure_reaches_on_error(db_url, monkeypatch) -> None:
    seen: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url,
        max_age=1.0,
        background_retention=True,
        retention_interval=0.01,
        on_error=seen.append,
    )
    try:
        monkeypatch.setattr(
            audit.retention, "run_once", lambda: (_ for _ in ()).throw(RuntimeError("prune-fail"))
        )
        for _ in range(500):
            if seen:
                break
            time.sleep(0.01)
        assert seen and "prune-fail" in str(seen[0])
    finally:
        audit.close()


def test_key_without_activation_refuses_plain_pruning(db_url: str) -> None:
    seen: list[BaseException] = []
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, max_age=3600.0, on_error=seen.append)
    try:
        audit.record("old")
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(created_at=now_utc() - timedelta(hours=2)))
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_no_activation is True
        assert len(_rows(audit.engine)) == 1
        assert seen and "activation marker" in str(seen[0])
    finally:
        audit.close()


def test_aligned_prune_writes_floor_and_prunes_old_seals(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old0", "old1", "old2")
        old_max = _rows(audit.engine)[-1].id
        for action in ("new0", "new1"):
            audit.record(action)
        audit.sealer.run_once()
        assert audit.retention.run_once() == 3
        assert [row.action for row in _rows(audit.engine)] == ["new0", "new1"]
        floors = [record for record in _records(audit.engine) if record.kind == "floor"]
        assert len(floors) == 1 and floors[0].to_id == old_max
        assert all(
            record.kind != "seal" or record.to_id > old_max for record in _records(audit.engine)
        )
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_aligned_prune_failure_before_floor_rolls_back(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old0", "old1")
        with (
            patch(
                "firm.audit.retention.HmacSigner.sign",
                side_effect=RuntimeError("floor-fail"),
            ),
            pytest.raises(RuntimeError, match="floor-fail"),
        ):
            audit.retention.run_once()
        assert len(_rows(audit.engine)) == 2
        assert not any(record.kind == "floor" for record in _records(audit.engine))
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_anchor_floor_line_precedes_committed_floor(db_url: str, tmp_path, at_time) -> None:
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        anchor_path=str(anchor),
        anchor_max_age=36000.0,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        assert audit.retention.run_once() == 1
        assert anchor.read_text(encoding="utf-8").splitlines()[-1].split()[1] == "FLOOR"
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_anchor_failure_refuses_prune(db_url: str, tmp_path, at_time) -> None:
    errors: list[BaseException] = []
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        anchor_path=str(anchor),
        on_error=errors.append,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        anchor.unlink()
        anchor.mkdir()
        assert audit.retention.run_once() == 0
        assert len(_rows(audit.engine)) == 1
        assert not any(record.kind == "floor" for record in _records(audit.engine))
        assert errors
    finally:
        audit.close()


def _fail_next_seals_insert(engine, exc_factory):
    """Arm a one-shot failure on the next INSERT into firm_audit_seals (the floor-row write,
    which in an aligned prune happens right *after* the anchor FLOOR line was fsynced)."""
    armed = {"on": True}

    def explode(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if armed["on"] and "insert into firm_audit_seals" in " ".join(statement.lower().split()):
            armed["on"] = False
            raise exc_factory()

    event.listen(engine, "before_cursor_execute", explode)
    return lambda: event.remove(engine, "before_cursor_execute", explode)


def test_interrupted_anchored_prune_is_pending_not_tampered(db_url: str, tmp_path, at_time) -> None:
    """A crash between the anchor FLOOR append and the database commit leaves the anchor floor
    leading the database, with rows and covering seals still present. That used to wedge both
    retention and verify into a permanent false TAMPERED; it is an interrupted prune: verify
    warns, and the next retention run resumes and completes it."""
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        anchor_path=str(anchor),
        anchor_max_age=36000.0,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old0", "old1")
        old_max = _rows(audit.engine)[-1].id

        remove = _fail_next_seals_insert(audit.engine, lambda: RuntimeError("crashed pre-commit"))
        try:
            with pytest.raises(RuntimeError, match="crashed pre-commit"):
                audit.retention.run_once()
        finally:
            remove()

        # The wedge state: anchor has the FLOOR line, the database rolled back completely.
        assert any(" FLOOR " in line for line in anchor.read_text(encoding="utf-8").splitlines())
        assert not any(record.kind == "floor" for record in _records(audit.engine))
        assert len(_rows(audit.engine)) == 2

        report = audit.verify(full=True)
        assert report.outcome == "warning"
        assert any("interrupted" in finding.message for finding in report.findings)

        # The next retention run resumes the prune instead of refusing as tampered.
        assert audit.retention.run_once() == 2
        assert audit.retention.last_refused_tampered == 0
        assert _rows(audit.engine) == []
        floors = [record for record in _records(audit.engine) if record.kind == "floor"]
        assert len(floors) == 1 and floors[0].to_id == old_max
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_interrupted_anchored_prune_retry_resumes_within_run(
    db_url: str, tmp_path, at_time
) -> None:
    """The retryable-rollback variant of the same wedge: the serialization retry inside
    run_once used to be defeated by its own anchor FLOOR line; now the retry resumes."""
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        anchor_path=str(anchor),
        anchor_max_age=36000.0,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old0", "old1")

        remove = _fail_next_seals_insert(
            audit.engine,
            lambda: OperationalError("prune", {}, RuntimeError("serialization failure")),
        )
        try:
            assert audit.retention.run_once() == 2
        finally:
            remove()
        assert audit.retention.last_refused_tampered == 0
        assert _rows(audit.engine) == []
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_restore_below_committed_floor_still_tampered(db_url: str, tmp_path, at_time) -> None:
    """Fail-closed guard around the interrupted-prune reconciliation: after a *committed*
    anchored prune, deleting the database floor row and re-inserting history (a forged
    "restore" — the covering seals are gone) must still verify as tampered."""
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        anchor_path=str(anchor),
        anchor_max_age=36000.0,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        assert audit.retention.run_once() == 1
        assert audit.verify(full=True).outcome == "ok"

        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.kind == "floor"))
            conn.execute(_audits.insert().values(id=1, action="forged", created_at=now_utc()))

        assert audit.verify(full=True).outcome == "tampered"
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered >= 1
    finally:
        audit.close()


def test_floor_advances_are_append_only_and_monotonic(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old.1")
        assert audit.retention.run_once() == 1
        first = next(record for record in _records(audit.engine) if record.kind == "floor")
        _seal_old(audit, at_time, "old.2")
        assert audit.retention.run_once() == 1
        floors = [record for record in _records(audit.engine) if record.kind == "floor"]
        assert len(floors) == 2
        assert floors[0].id == first.id
        assert floors[0].to_id < floors[1].to_id
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_sqlite_ids_remain_above_floor_after_table_empties(
    db_url: str, at_time, is_sqlite: bool
) -> None:
    if not is_sqlite:
        pytest.skip("SQLite AUTOINCREMENT regression")
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        old_id = _rows(audit.engine)[0].id
        assert audit.retention.run_once() == 1
        audit.record("new")
        assert _rows(audit.engine)[0].id > old_id
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_prune_refuses_unsealed_rows_and_reports_skip(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "sealed")
        old = now_utc() - timedelta(hours=2)
        with at_time(old):
            audit.record("unsealed")
        assert audit.retention.run_once() == 1
        assert audit.retention.last_skipped_unsealed == 1
        assert [row.action for row in _rows(audit.engine)] == ["unsealed"]
    finally:
        audit.close()


def test_prune_refuses_tampered_seal_mac(db_url: str, at_time) -> None:
    seen: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        on_error=seen.append,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        with transaction(audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.kind == "seal").values(seal_mac="0" * 64))
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered == 1
        assert len(_rows(audit.engine)) == 1
        assert seen and "REFUSED" in str(seen[0])
    finally:
        audit.close()


def test_prune_refuses_tampered_row(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(action="HACKED"))
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered == 1
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_prune_refuses_surplus_valid_mac_row_in_sealed_range(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        old = now_utc() - timedelta(hours=2)
        with at_time(old):
            for action in ("a", "gap", "c"):
                audit.record(action)
        rows = _rows(audit.engine)
        gap_id = rows[1].id
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == gap_id))
        with patch("firm.audit.sealing.now_utc", lambda: old):
            assert audit.sealer.run_once() == 2
        with at_time(old):
            audit.record("late")
        extra = _rows(audit.engine)[-1]
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == extra.id).values(id=gap_id))
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered == 1
    finally:
        audit.close()


def test_prune_refuses_created_at_mutation(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        audit.record("fresh")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(created_at=now_utc() - timedelta(hours=2)))
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_seals)
                .where(_seals.c.kind == "seal")
                .values(sealed_at=now_utc() - timedelta(hours=2))
            )
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered == 1
    finally:
        audit.close()


def test_retention_without_split_seal_key_refuses_loudly(db_url: str, at_time) -> None:
    owner = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        seal_key=_SEAL_SECRET,
        grace=0.0,
        max_age=3600.0,
    )
    try:
        _activate(owner)
        _seal_old(owner, at_time, "old")
        seen: list[BaseException] = []
        pruner = AuditLog(
            engine=owner.engine,
            create_schema=False,
            mac_key=_SECRET,
            max_age=3600.0,
            on_error=seen.append,
        )
        try:
            assert pruner.retention.run_once() == 0
            assert pruner.retention.last_refused_no_seal_key is True
            assert seen and "seal key" in str(seen[0])
        finally:
            pruner.close()
    finally:
        owner.close()


def test_large_unsealed_skip_routes_to_on_error(db_url: str, at_time, monkeypatch) -> None:
    monkeypatch.setattr("firm.audit.retention._SKIP_ALERT_THRESHOLD", 0)
    seen: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        on_error=seen.append,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "sealed")
        old = now_utc() - timedelta(hours=2)
        with at_time(old):
            audit.record("unsealed")
        audit.retention.run_once()
        assert any("UNSEALED" in str(error) for error in seen)
    finally:
        audit.close()


def test_aligned_prune_retries_serialization_failure(db_url: str, at_time, monkeypatch) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        original = audit.retention._run_aligned_once
        attempts = 0

        def flaky(cutoff):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("prune", {}, RuntimeError("serialization failure"))
            return original(cutoff)

        monkeypatch.setattr(audit.retention, "_run_aligned_once", flaky)
        assert audit.retention.run_once() == 1
        assert attempts == 2
    finally:
        audit.close()


def test_retention_locks_activation_before_loading_floor_state(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")
        event.listen(audit.engine, "before_cursor_execute", capture)
        assert audit.retention._run_aligned_once(now_utc() - timedelta(hours=1)) == 1
    finally:
        event.remove(audit.engine, "before_cursor_execute", capture)
        audit.close()
    activation = next(
        index
        for index, statement in enumerate(statements)
        if "from firm_audit_seals" in statement and "from_id =" in statement
    )
    side_table_page = next(
        index
        for index, statement in enumerate(statements)
        if "substr(cast(firm_audit_seals.kind" in statement
    )
    assert activation < side_table_page


def test_persistent_serialization_failure_is_reported_not_raised(
    db_url: str, at_time, monkeypatch
) -> None:
    errors: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        max_age=3600.0,
        on_error=errors.append,
    )
    try:
        _activate(audit)
        _seal_old(audit, at_time, "old")

        def fail(_cutoff):
            raise OperationalError("prune", {}, RuntimeError("deadlock detected"))

        monkeypatch.setattr(audit.retention, "_run_aligned_once", fail)
        assert audit.retention.run_once() == 0
        assert len(_rows(audit.engine)) == 1
        assert len(errors) == 1
    finally:
        audit.close()
