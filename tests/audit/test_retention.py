"""Retention: keep-forever default, age-based pruning, and the background loop.

The second half covers the sealing-aligned path (design "Retention integration" / D15): pruning
only fully-sealed ranges, the checkpoint seal that carries the chain forward, the pruning of the
subsumed old seals, verify staying OK across the checkpoint, and the loud skip of expired-but-
unsealed rows.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema
from firm.audit.integrity import load_key
from firm.audit.retention import RetentionLoop

_SECRET = "retention-secret-key-padding-0123456789"  # noqa: S105  (>= 32 chars)
_KEY = load_key(_SECRET)
assert _KEY is not None

_audits = schema.audit_events
_seals = schema.seals


def test_keep_forever_default_is_a_noop(audit: AuditLog) -> None:
    audit.record("a")
    audit.record("b")
    assert audit.max_age is None
    assert audit.retention.run_once() == 0
    assert len(audit.history()) == 2


def test_prune_deletes_only_rows_older_than_max_age(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        audit.record("new")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(schema.audit_events)
                .where(schema.audit_events.c.action == "old")
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        assert audit.retention.run_once() == 1
        rows = audit.history()
        assert [r["action"] for r in rows] == ["new"]
    finally:
        audit.close()


def test_recording_never_triggers_retention(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=0.001)
    try:
        audit.record("a")
        time.sleep(0.05)
        audit.record("b")
        # max_age is aggressively short, but record() never calls into retention itself.
        assert len(audit.history()) == 2
    finally:
        audit.close()


def test_background_retention_flag_starts_loop(db_url: str) -> None:
    audit = AuditLog(
        database_url=db_url, max_age=3600.0, background_retention=True, retention_interval=0.05
    )
    try:
        assert audit._loop is not None
        assert audit._loop.name == "audit-retention"
    finally:
        audit.close()


def test_retention_loop_runs_a_pass(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(schema.audit_events)
                .where(schema.audit_events.c.action == "old")
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        loop = RetentionLoop(audit.retention, interval=0.05)
        loop.start()
        try:
            for _ in range(40):
                if not audit.history():
                    break
                time.sleep(0.05)
            assert audit.history() == []
        finally:
            loop.stop()
    finally:
        audit.close()


def test_background_retention_failure_reaches_on_error(db_url, monkeypatch) -> None:
    """X-1: RetentionLoop failures now route to AuditLog(on_error=...)."""
    import time

    from firm.audit import AuditLog

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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.01)
        assert seen and "prune-fail" in str(seen[0])
    finally:
        audit.close()


# -- sealing-aligned retention (design "Retention integration" / D15) ---------------------------


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _seals_by_seq(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.seq)).all()


def _age(engine, *, upto_id: int, seconds: float) -> None:
    with transaction(engine) as conn:
        conn.execute(
            update(_audits).where(_audits.c.id <= upto_id).values(
                created_at=now_utc() - timedelta(seconds=seconds))
        )


def test_key_but_no_seals_uses_plain_pruning(db_url: str) -> None:
    # Sealing is only "active" once a seal exists; with a key but no seals, pruning is unchanged.
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        audit.record("old")
        audit.record("keep")
        _age(audit.engine, upto_id=_rows(audit.engine)[0].id, seconds=7200)
        assert audit.retention.run_once() == 1
        assert [r.action for r in _rows(audit.engine)] == ["keep"]
        assert _seals_by_seq(audit.engine) == []  # no checkpoint written on the plain path
    finally:
        audit.close()


def test_aligned_prune_writes_checkpoint_and_prunes_old_seals(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):  # genuinely-old, signed at the past
            for i in range(3):
                audit.record(f"old{i}")
        audit.sealer.run_once()  # seq 1 covers ids 1..3 (expired, and still verify)
        old_max = _rows(audit.engine)[-1].id
        for i in range(3):
            audit.record(f"new{i}")  # fresh, current time
        audit.sealer.run_once()  # seq 2 covers ids 4..6 (fresh, not expired)

        deleted = audit.retention.run_once()
        assert deleted == 3
        assert audit.retention.last_skipped_unsealed == 0
        assert audit.retention.last_refused_tampered == 0
        # Only the fresh rows survive.
        assert [r.action for r in _rows(audit.engine)] == ["new0", "new1", "new2"]
        # A checkpoint recorded the pruned boundary, and the subsumed seq-1 seal is gone.
        seals = _seals_by_seq(audit.engine)
        kinds = {s.seq: s.kind for s in seals}
        assert 1 not in kinds  # seq 1 pruned (subsumed by the checkpoint)
        assert 2 in kinds and kinds[2] == "seal"
        checkpoint = next(s for s in seals if s.kind == "checkpoint")
        assert checkpoint.to_id == old_max
    finally:
        audit.close()


def test_verify_stays_ok_across_a_checkpoint(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(3):
                audit.record(f"old{i}")
        audit.sealer.run_once()
        for i in range(3):
            audit.record(f"new{i}")
        audit.sealer.run_once()

        audit.retention.run_once()  # prune + checkpoint
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.ok_count == 3  # the three surviving rows recompute cleanly
    finally:
        audit.close()


def test_checkpoint_pruning_the_anchored_seal_keeps_verify_ok(
    db_url: str, tmp_path, at_time
) -> None:
    # A low-volume log: seal seq 1 is anchored, then its rows age out and retention checkpoints
    # seq 1 away. The checkpoint must be exported to the anchor too — otherwise the next
    # `verify --anchor` reads the pruned-away anchored seq 1 as a tail truncation (false TAMPERED).
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0,
                     anchor_path=str(anchor))
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            audit.record("only")
        audit.sealer.run_once()  # seq 1, anchored
        audit.retention.run_once()  # checkpoint seq 2 prunes seq 1 and its row
        # The checkpoint was appended to the anchor, so the newest anchored seq is in the chain.
        assert len(anchor.read_text(encoding="utf-8").splitlines()) == 2
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
    finally:
        audit.close()


def test_anchor_naming_a_pruned_below_floor_seal_is_not_a_truncation(
    db_url: str, tmp_path, at_time
) -> None:
    # Even if the checkpoint's best-effort anchor write was lost, an anchored seq at or below the
    # checkpoint floor was legitimately pruned — a key-signed checkpoint (re-verified by the chain
    # walk) vouches for its absence, so it is not a tail truncation.
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0,
                     anchor_path=str(anchor))
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            audit.record("only")
        audit.sealer.run_once()  # seq 1, anchor line 1
        first_line = anchor.read_text(encoding="utf-8").splitlines()[0]
        audit.retention.run_once()  # checkpoint seq 2 prunes seq 1
        # Simulate the checkpoint's anchor write having been lost: newest line still names seq 1.
        anchor.write_text(first_line + "\n", encoding="utf-8")
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
    finally:
        audit.close()


def test_prune_refuses_unsealed_rows_and_reports_the_skip(db_url: str, at_time) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(3):
                audit.record(f"sealed{i}")
        audit.sealer.run_once()  # seq 1 covers the first three (expired, still verify)
        # Three expired but UNSEALED rows (raw inserts past the last seal).
        with transaction(audit.engine) as conn:
            for i in range(3):
                conn.execute(_audits.insert().values(
                    action=f"unsealed{i}", created_at=now_utc() - timedelta(seconds=7200)))

        deleted = audit.retention.run_once()
        assert deleted == 3  # only the sealed, expired rows
        assert audit.retention.last_skipped_unsealed == 3  # the unsealed expired rows are reported
        assert audit.retention.last_refused_tampered == 0  # nothing tampered, just unsealed
        remaining = {r.action for r in _rows(audit.engine)}
        assert remaining == {"unsealed0", "unsealed1", "unsealed2"}  # unsealed rows survive
    finally:
        audit.close()


def test_prune_refuses_a_tampered_sealed_range(db_url: str, at_time) -> None:
    # The review finding (#3): an attacker edits an old *sealed* row's content with a plain UPDATE
    # (no key needed) and leaves its row_mac column untouched. rows_mac hashes the stored MAC
    # strings, so it still matches the seal — only recomputing each row's MAC from its content
    # catches the edit. Once the row ages past max_age a naive prune would delete it and checkpoint
    # over it, and verify would then report OK. Retention must instead REFUSE and preserve it.
    seen: list[BaseException] = []
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0,
                     on_error=seen.append)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            audit.record("range1.a")
            audit.record("range1.b")
        audit.sealer.run_once()  # seq 1 covers ids 1..2 (expired)
        with at_time(now_utc() - timedelta(seconds=7200)):
            audit.record("range2.a")
        audit.sealer.run_once()  # seq 2 covers id 3 (also expired, but untampered)

        # Tamper a sealed row's content — a real UPDATE, row_mac left as-is.
        with transaction(audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.action == "range1.a")
                         .values(action="range1.a.TAMPERED"))

        deleted = audit.retention.run_once()
        assert deleted == 0  # refused — nothing is pruned
        assert audit.retention.last_refused_tampered == 1
        assert seen and "REFUSED" in str(seen[0])  # the refusal is loud (on_error)

        # Stopping semantics: the boundary halts at the first refused range, so the checkpoint never
        # advances (no checkpoint seal) and the *later* clean range 2 is not pruned either — every
        # row, tampered and clean, stays in place. Skipping past the tamper would be more permissive
        # but would let the evidence be pruned on a later run; stopping keeps it until we act.
        seals = _seals_by_seq(audit.engine)
        assert all(s.kind == "seal" for s in seals)  # no checkpoint written
        assert {r.action for r in _rows(audit.engine)} == {
            "range1.a.TAMPERED", "range1.b", "range2.a"}

        # And the evidence is not laundered: a subsequent verify still surfaces the tampering.
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_prune_refuses_a_created_at_mutated_row(db_url: str) -> None:
    # The explicit post-insert `created_at` mutation case: the old aging trick is itself a
    # MAC-invalidating edit (created_at is bound into row_mac). Retention must REFUSE such a range,
    # not prune it — which is exactly why the aging convention moved to signed past-dated inserts.
    seen: list[BaseException] = []
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0,
                     on_error=seen.append)
    try:
        audit.record("fresh.a")
        audit.record("fresh.b")
        audit.sealer.run_once()  # seq 1 covers ids 1..2 (not yet expired)
        # Mutate created_at into the past: this both expires the range and breaks its row MACs.
        _age(audit.engine, upto_id=_rows(audit.engine)[-1].id, seconds=7200)

        deleted = audit.retention.run_once()
        assert deleted == 0
        assert audit.retention.last_refused_tampered == 1
        assert seen and "REFUSED" in str(seen[0])
        assert len(_rows(audit.engine)) == 2  # the evidence is preserved, not pruned
    finally:
        audit.close()


def test_large_unsealed_skip_routes_to_on_error(db_url: str, monkeypatch) -> None:
    from firm.audit import retention as retention_mod

    monkeypatch.setattr(retention_mod, "_SKIP_ALERT_THRESHOLD", 2)
    seen: list[BaseException] = []
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0,
                     on_error=seen.append)
    try:
        audit.record("sealed")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            for i in range(3):  # 3 > threshold 2
                conn.execute(_audits.insert().values(
                    action=f"unsealed{i}", created_at=now_utc() - timedelta(seconds=7200)))
        audit.retention.run_once()
        assert seen and "UNSEALED" in str(seen[0])
    finally:
        audit.close()
