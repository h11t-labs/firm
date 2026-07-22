"""Independent sealing, explicit activation, races, batching, and anchor emission."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import delete, func, select

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, integrity, schema
from firm.audit.integrity import load_key

_SECRET = "sealing-secret-key-padding-0123456789"  # noqa: S105
_KEY = load_key(_SECRET)
assert _KEY is not None
_audits = schema.audit_events
_seals = schema.seals


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FIRM_AUDIT_KEY", "FIRM_AUDIT_ANCHOR_PATH"):
        monkeypatch.delenv(name, raising=False)


def _records(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.id)).all()


def _range_seals(engine) -> list:
    return [record for record in _records(engine) if record.kind == "seal"]


def _activate(audit: AuditLog) -> None:
    assert audit.sealer.run_once() == 0
    assert len([record for record in _records(audit.engine) if record.kind == "activation"]) == 1


def _pairs(engine, *, after: int = 0, upto: int | None = None) -> list[tuple[int, str]]:
    with transaction(engine) as conn:
        stmt = select(_audits.c.id, _audits.c.row_mac).where(_audits.c.id > after)
        if upto is not None:
            stmt = stmt.where(_audits.c.id <= upto)
        rows = conn.execute(stmt.order_by(_audits.c.id)).all()
    assert all(row.row_mac is not None for row in rows)
    return [(row.id, row.row_mac) for row in rows]


def _max_id(engine) -> int:
    with transaction(engine) as conn:
        return conn.execute(select(func.max(_audits.c.id))).scalar_one() or 0


def _recompute(record) -> str:
    return integrity.seal_mac(
        _KEY,
        from_id=record.from_id,
        to_id=record.to_id,
        row_count=record.row_count,
        rows_mac=record.rows_mac,
        sealed_at=record.sealed_at,
        key_id=record.key_id,
    )


def test_normal_seal_covers_all_post_activation_rows(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        _activate(audit)
        for action in ("a", "b", "c"):
            audit.record(action)
        assert audit.sealer.run_once() == 3
        (seal,) = _range_seals(audit.engine)
        assert seal.from_id == 0
        assert seal.to_id == _max_id(audit.engine)
        assert seal.row_count == 3
        assert seal.key_id == _KEY.id
        assert integrity.rows_mac(_KEY, _pairs(audit.engine, upto=seal.to_id)) == seal.rows_mac
        assert _recompute(seal) == seal.seal_mac
    finally:
        audit.close()


def test_no_new_rows_is_a_noop(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        _activate(audit)
        audit.record("a")
        assert audit.sealer.run_once() == 1
        assert audit.sealer.run_once() == 0
        assert len(_range_seals(audit.engine)) == 1
    finally:
        audit.close()


def test_no_key_sealer_is_a_noop(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 0
        assert _records(audit.engine) == []
    finally:
        audit.close()


def test_backlog_larger_than_batch_becomes_multiple_seals(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, seal_batch_size=2)
    try:
        _activate(audit)
        for index in range(5):
            audit.record(f"e{index}")
        assert audit.sealer.run_once() == 5
        seals = _range_seals(audit.engine)
        assert [seal.row_count for seal in seals] == [2, 2, 1]
        assert seals[0].from_id == 0
        for previous, current in pairwise(seals):
            assert current.from_id == previous.to_id
        for seal in seals:
            assert (
                integrity.rows_mac(_KEY, _pairs(audit.engine, after=seal.from_id, upto=seal.to_id))
                == seal.rows_mac
            )
    finally:
        audit.close()


def test_resume_from_hwm_after_partial_progress(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        _activate(audit)
        audit.record("a")
        audit.record("b")
        assert audit.sealer.run_once() == 2
        first = _range_seals(audit.engine)[0]
        audit.record("c")
        assert audit.sealer.run_once() == 1
        second = _range_seals(audit.engine)[1]
        assert second.from_id == first.to_id
        assert second.row_count == 1
    finally:
        audit.close()


def test_rollback_id_gap_inside_range_seals_what_exists(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        _activate(audit)
        for action in ("a", "b", "c"):
            audit.record(action)
        gap_id = _pairs(audit.engine)[1][0]
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == gap_id))
        assert audit.sealer.run_once() == 2
        (seal,) = _range_seals(audit.engine)
        assert seal.row_count == 2
        assert integrity.rows_mac(_KEY, _pairs(audit.engine, upto=seal.to_id)) == seal.rows_mac
    finally:
        audit.close()


def test_null_mac_above_activation_is_refused_not_hashed(db_url: str) -> None:
    errors: list[BaseException] = []
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, on_error=errors.append)
    try:
        _activate(audit)
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="unsigned", created_at=now_utc()))
        assert audit.sealer.run_once() == 0
        assert _range_seals(audit.engine) == []
        assert errors and "unsigned" in str(errors[0])
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_first_pass_records_activation_boundary_without_sealing_legacy(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("pre-signed")
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="pre-legacy", created_at=now_utc()))
        boundary = _max_id(audit.engine)
        assert audit.sealer.run_once() == 0
        activation = next(
            record for record in _records(audit.engine) if record.kind == "activation"
        )
        assert activation.to_id == boundary
        assert _range_seals(audit.engine) == []
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.unprotected_count == 1
    finally:
        audit.close()


def test_grace_excludes_young_rows(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=3600.0)
    try:
        _activate(audit)
        audit.record("young")
        assert audit.sealer.run_once() == 0
        with at_time(now_utc() - timedelta(hours=2)):
            audit.record("old")
        assert audit.sealer.run_once() == 1
        assert [row["action"] for row in audit.history()] == ["old", "young"]
    finally:
        audit.close()


def test_two_concurrent_sealers_keep_ranges_contiguous(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, seal_batch_size=2)
    try:
        _activate(audit)
        for index in range(20):
            audit.record(f"e{index}")
        target = _max_id(audit.engine)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(400):
                    audit.sealer.run_once()
                    seals = _range_seals(audit.engine)
                    if seals and seals[-1].to_id == target:
                        return
                    time.sleep(0.001)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        seals = _range_seals(audit.engine)
        assert seals[0].from_id == 0
        assert all(current.from_id == previous.to_id for previous, current in pairwise(seals))
        assert seals[-1].to_id == target
        assert sum(seal.row_count for seal in seals) == 20
    finally:
        audit.close()


def test_anchor_file_uses_new_format_for_activation_and_seals(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        seal_batch_size=1,
        anchor_path=str(anchor),
    )
    try:
        _activate(audit)
        audit.record("a")
        audit.record("b")
        audit.sealer.run_once()
        lines = anchor.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert lines[0].split()[1] == "ACTIVATION"
        assert all(line.split()[1] == "SEAL" for line in lines[1:])
        assert all(len(line.split()) == 5 for line in lines[1:])
    finally:
        audit.close()


def test_on_anchor_callback_receives_each_event(db_url: str) -> None:
    seen: list[tuple[str, int | None, int, str, datetime]] = []
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        on_anchor=lambda *event: seen.append(event),
    )
    try:
        _activate(audit)
        audit.record("a")
        audit.sealer.run_once()
        assert [event[0] for event in seen] == ["activation", "seal"]
        assert all(isinstance(event[4], datetime) for event in seen)
    finally:
        audit.close()


def test_anchor_write_failure_routes_to_on_error_and_records_commit(db_url: str, tmp_path) -> None:
    errors: list[BaseException] = []
    bad_path = tmp_path / "anchor-dir"
    bad_path.mkdir()
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        anchor_path=str(bad_path),
        on_error=errors.append,
    )
    try:
        _activate(audit)
        audit.record("a")
        assert audit.sealer.run_once() == 1
        assert len(_range_seals(audit.engine)) == 1
        assert errors and all(isinstance(error, OSError) for error in errors)
    finally:
        audit.close()


def test_on_anchor_callback_failure_routes_to_on_error(db_url: str) -> None:
    errors: list[BaseException] = []

    def boom(*_event) -> None:
        raise RuntimeError("anchor-sink-down")

    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, on_anchor=boom, on_error=errors.append
    )
    try:
        _activate(audit)
        assert errors and "anchor-sink-down" in str(errors[0])
    finally:
        audit.close()


def test_background_sealing_warns_and_starts_the_loop(db_url: str) -> None:
    with pytest.warns(UserWarning, match="grace"):
        audit = AuditLog(
            database_url=db_url, mac_key=_SECRET, background_sealing=True, seal_interval=0.05
        )
    try:
        assert audit._seal_loop is not None
        assert audit._seal_loop.name == "audit-sealer"
    finally:
        audit.close()


def test_background_sealing_without_a_key_warns_loudly(db_url: str) -> None:
    with pytest.warns(UserWarning, match="no seal key is configured"):
        audit = AuditLog(database_url=db_url, background_sealing=True, seal_interval=0.05)
    audit.close()


def test_seal_loop_runs_a_pass(db_url: str) -> None:
    with pytest.warns(UserWarning):
        audit = AuditLog(
            database_url=db_url,
            mac_key=_SECRET,
            grace=0.0,
            background_sealing=True,
            seal_interval=0.02,
        )
    try:
        for _ in range(100):
            if any(record.kind == "activation" for record in _records(audit.engine)):
                break
            time.sleep(0.02)
        audit.record("a")
        audit.record("b")
        for _ in range(250):
            seals = _range_seals(audit.engine)
            if sum(seal.row_count for seal in seals) == 2:
                break
            time.sleep(0.02)
        seals = _range_seals(audit.engine)
        assert sum(seal.row_count for seal in seals) == 2
        assert all(current.from_id == previous.to_id for previous, current in pairwise(seals))
    finally:
        audit.close()
