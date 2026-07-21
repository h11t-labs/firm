"""Layer-2 sealing: :class:`Sealer.run_once`, the batching/backlog drain, the concurrent-sealer
race, the activation boundary, NULL-MAC handling, and the best-effort anchor sink.

Verify (design step 5) doesn't exist yet, so these tests check the sealer's output *at the data
level*: they recompute ``rows_mac``/``seal_mac`` from the rows actually present with the same
:mod:`firm.audit.integrity` helpers the sealer used, which is exactly what a later verifier will
do. Everything runs on SQLite by default and on Postgres/MySQL when their ``FIRM_TEST_*`` URLs are
set (the concurrent-sealer test exercises the ``seq`` unique-constraint race on each).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import delete, func, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, integrity, schema
from firm.audit.integrity import load_key

# A valid throwaway writer key (>= 32 chars).
_SECRET = "sealing-secret-key-padding-0123456789"  # noqa: S105
_KEY = load_key(_SECRET)
assert _KEY is not None

_audits = schema.audit_events
_seals = schema.seals


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a stray ``FIRM_AUDIT_KEY`` / ``FIRM_AUDIT_ANCHOR_PATH`` in the environment out of the
    tests — each test configures the key and anchor explicitly."""
    monkeypatch.delenv("FIRM_AUDIT_KEY", raising=False)
    monkeypatch.delenv("FIRM_AUDIT_ANCHOR_PATH", raising=False)


# -- data-level helpers -------------------------------------------------------------------------


def _seal_rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.seq)).all()


def _pairs(engine, *, after: int = 0, upto: int | None = None) -> list[tuple[int, str | None]]:
    """The ``(id, row_mac)`` pairs present in ``(after, upto]`` in id order — what a seal hashes."""
    with transaction(engine) as conn:
        stmt = select(_audits.c.id, _audits.c.row_mac).where(_audits.c.id > after)
        if upto is not None:
            stmt = stmt.where(_audits.c.id <= upto)
        rows = conn.execute(stmt.order_by(_audits.c.id)).all()
    return [(row.id, row.row_mac) for row in rows]


def _max_id(engine) -> int | None:
    with transaction(engine) as conn:
        return conn.execute(select(func.max(_audits.c.id))).scalar()


def _recompute_seal_mac(seal) -> str:
    return integrity.seal_mac(
        _KEY,
        seq=seal.seq,
        kind=seal.kind,
        from_id=seal.from_id,
        to_id=seal.to_id,
        row_count=seal.row_count,
        rows_mac=seal.rows_mac,
        prev_mac=seal.prev_mac,
        sealed_at=seal.sealed_at,
    )


# -- normal sealing -----------------------------------------------------------------------------


def test_normal_seal_covers_all_rows_from_genesis(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("a")
        audit.record("b")
        audit.record("c")
        assert audit.sealer.run_once() == 3

        (seal,) = _seal_rows(audit.engine)
        assert seal.seq == 1
        assert seal.kind == "seal"
        assert seal.from_id == 0
        assert seal.to_id == _max_id(audit.engine)
        assert seal.row_count == 3
        assert seal.prev_mac == "genesis"
        assert seal.key_id == _KEY.id
        # rows_mac and seal_mac recompute from the rows present and the seal's own fields.
        assert integrity.rows_mac(_KEY, _pairs(audit.engine, upto=seal.to_id)) == seal.rows_mac
        assert _recompute_seal_mac(seal) == seal.seal_mac
    finally:
        audit.close()


def test_no_new_rows_is_a_noop(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 1
        # A second pass with nothing new seals nothing and adds no seal (idempotent).
        assert audit.sealer.run_once() == 0
        assert len(_seal_rows(audit.engine)) == 1
    finally:
        audit.close()


def test_no_key_sealer_is_a_noop(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)  # no key: sealing has nothing to sign with
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 0
        assert _seal_rows(audit.engine) == []
    finally:
        audit.close()


# -- batching / backlog drain (review 7A) -------------------------------------------------------


def test_backlog_larger_than_batch_becomes_multiple_seals(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, seal_batch_size=2)
    try:
        for i in range(5):
            audit.record(f"e{i}")
        assert audit.sealer.run_once() == 5

        seals = _seal_rows(audit.engine)
        assert [s.seq for s in seals] == [1, 2, 3]
        assert [s.row_count for s in seals] == [2, 2, 1]  # 2 + 2 + 1, never one monster txn
        # Dense, contiguous, chained.
        assert seals[0].from_id == 0
        assert seals[0].prev_mac == "genesis"
        for prev, cur in pairwise(seals):
            assert cur.from_id == prev.to_id
            assert cur.prev_mac == prev.seal_mac
        # Every seal's rows_mac matches the rows in its own range.
        for seal in seals:
            in_range = _pairs(audit.engine, after=seal.from_id, upto=seal.to_id)
            assert integrity.rows_mac(_KEY, in_range) == seal.rows_mac
    finally:
        audit.close()


# -- crash-mid-seal resume (idempotent via hwm) -------------------------------------------------


def test_resume_from_hwm_after_partial_progress(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("a")
        audit.record("b")
        assert audit.sealer.run_once() == 2
        (first,) = _seal_rows(audit.engine)

        # A "crash" leaves the first seal committed; a fresh run resumes from its to_id (the hwm)
        # and only seals what arrived since — no re-sealing, no gap.
        audit.record("c")
        assert audit.sealer.run_once() == 1
        seals = _seal_rows(audit.engine)
        assert len(seals) == 2
        assert seals[1].seq == 2
        assert seals[1].from_id == first.to_id
        assert seals[1].prev_mac == first.seal_mac
        assert seals[1].row_count == 1
    finally:
        audit.close()


# -- rollback id-gap inside a range -------------------------------------------------------------


def test_rollback_id_gap_inside_range_seals_what_exists(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("a")
        audit.record("b")
        audit.record("c")
        # Simulate a rolled-back insert: a hole in the id sequence inside the range to be sealed.
        gap_id = _pairs(audit.engine)[1][0]
        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == gap_id))

        assert audit.sealer.run_once() == 2  # two rows actually present
        (seal,) = _seal_rows(audit.engine)
        assert seal.row_count == 2
        # The seal hashes the rows present, never assumes id continuity — recompute is clean.
        assert integrity.rows_mac(_KEY, _pairs(audit.engine, upto=seal.to_id)) == seal.rows_mac
    finally:
        audit.close()


# -- NULL-MAC rows are sealed with the nomac marker (review 5A) ----------------------------------


def test_null_mac_row_is_sealed_with_marker(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        audit.record("signed")  # carries a row_mac
        # A NULL-MAC row (a legacy row, or a straggler instance without the key): insert raw.
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="unsigned", created_at=now_utc()))

        assert audit.sealer.run_once() == 2
        (seal,) = _seal_rows(audit.engine)
        assert seal.row_count == 2
        present = _pairs(audit.engine, upto=seal.to_id)
        assert any(mac is None for _, mac in present)  # one row really is NULL-MAC
        assert integrity.rows_mac(_KEY, present) == seal.rows_mac
        # Deleting the NULL-MAC row still changes the seal — its deletion is detectable.
        remaining = [(rid, mac) for rid, mac in present if mac is not None]
        assert integrity.rows_mac(_KEY, remaining) != seal.rows_mac
    finally:
        audit.close()


# -- activation boundary (design "Layer 2 — seals", point 4 / D13) ------------------------------


def test_first_seal_records_activation_boundary(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0)
    try:
        # Rows that pre-exist activation — including a legacy NULL-MAC row.
        audit.record("pre-signed")
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="pre-legacy", created_at=now_utc()))
        audit.record("pre-signed-2")
        boundary = _max_id(audit.engine)

        # The first seal covers everything from the start of the table (from_id 0), sealing the
        # legacy row too; its to_id is the activation boundary a verifier reads.
        assert audit.sealer.run_once() == 3
        (seal,) = _seal_rows(audit.engine)
        assert seal.seq == 1
        assert seal.from_id == 0
        assert seal.to_id == boundary
        assert seal.prev_mac == "genesis"
    finally:
        audit.close()


# -- grace window -------------------------------------------------------------------------------


def test_grace_excludes_young_rows(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=3600.0)
    try:
        audit.record("young")
        assert audit.sealer.run_once() == 0  # inside the grace window: not yet sealable
        assert _seal_rows(audit.engine) == []

        # Age it past the grace window; now it is eligible.
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).values(created_at=now_utc() - timedelta(hours=2)))
        assert audit.sealer.run_once() == 1
        assert len(_seal_rows(audit.engine)) == 1
    finally:
        audit.close()


# -- two concurrent sealers race on the seq unique constraint -----------------------------------


def test_two_concurrent_sealers_keep_the_chain_dense(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, seal_batch_size=2)
    try:
        for i in range(20):
            audit.record(f"e{i}")
        target = _max_id(audit.engine)

        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(400):
                    audit.sealer.run_once()
                    seals = _seal_rows(audit.engine)
                    if seals and seals[-1].to_id == target:
                        return
                    time.sleep(0.001)
            except BaseException as exc:  # a race must never surface as a crash
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors  # the seq-race loser retries benignly, it never raises
        seals = _seal_rows(audit.engine)
        assert [s.seq for s in seals] == list(range(1, len(seals) + 1))  # dense seq
        assert seals[0].from_id == 0
        for prev, cur in pairwise(seals):
            assert cur.from_id == prev.to_id  # contiguous, no overlap or gap
            assert cur.prev_mac == prev.seal_mac  # chained
        assert seals[-1].to_id == target
        assert sum(s.row_count for s in seals) == 20  # each row sealed exactly once
    finally:
        audit.close()


# -- anchor sink (Layer 3, best-effort — review 3A) ---------------------------------------------


def test_anchor_file_is_appended_per_seal(db_url: str, tmp_path) -> None:
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, seal_batch_size=1, anchor_path=str(anchor)
    )
    try:
        audit.record("a")
        audit.record("b")
        audit.sealer.run_once()

        seals = _seal_rows(audit.engine)
        lines = anchor.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(seals) == 2
        for line, seal in zip(lines, seals, strict=True):
            parts = line.split()
            assert len(parts) == 3  # "<sealed_at> <seq> <seal_mac>"
            assert parts[1] == str(seal.seq)
            assert parts[2] == seal.seal_mac
    finally:
        audit.close()


def test_on_anchor_callback_receives_each_seal(db_url: str) -> None:
    seen: list[tuple[int, str, datetime]] = []
    audit = AuditLog(
        database_url=db_url,
        mac_key=_SECRET,
        grace=0.0,
        on_anchor=lambda seq, seal_mac, sealed_at: seen.append((seq, seal_mac, sealed_at)),
    )
    try:
        audit.record("a")
        audit.sealer.run_once()
        (seal,) = _seal_rows(audit.engine)
        assert [(seq, mac) for seq, mac, _ in seen] == [(seal.seq, seal.seal_mac)]
        assert isinstance(seen[0][2], datetime)
    finally:
        audit.close()


def test_anchor_write_failure_routes_to_on_error_seal_commits(db_url: str, tmp_path) -> None:
    errors: list[BaseException] = []
    # Point the anchor at a directory: open(..., "a") raises, exercising the best-effort path.
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
        audit.record("a")
        assert audit.sealer.run_once() == 1
        # The seal committed despite the anchor write failing...
        assert len(_seal_rows(audit.engine)) == 1
        # ...and the failure surfaced through on_error rather than vanishing or crashing.
        assert errors and isinstance(errors[0], OSError)
    finally:
        audit.close()


def test_on_anchor_callback_failure_routes_to_on_error(db_url: str) -> None:
    errors: list[BaseException] = []

    def boom(seq: int, seal_mac: str, sealed_at: datetime) -> None:
        raise RuntimeError("anchor-sink-down")

    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, on_anchor=boom, on_error=errors.append
    )
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 1
        assert len(_seal_rows(audit.engine)) == 1
        assert errors and "anchor-sink-down" in str(errors[0])
    finally:
        audit.close()


# -- SealLoop wiring ----------------------------------------------------------------------------


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
        audit.record("a")
        audit.record("b")
        for _ in range(100):
            if _seal_rows(audit.engine):
                break
            time.sleep(0.02)
        seals = _seal_rows(audit.engine)
        assert len(seals) == 1
        assert seals[0].row_count == 2
    finally:
        audit.close()
