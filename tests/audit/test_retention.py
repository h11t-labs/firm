"""Retention: keep-forever default, age-based pruning, and the background loop.

The second half covers the sealing-aligned path (design "Retention integration" / D15): pruning
only fully-sealed ranges, the checkpoint seal that carries the chain forward, the pruning of the
subsumed old seals, verify staying OK across the checkpoint, and the loud skip of expired-but-
unsealed rows.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import delete, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, integrity, schema
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
            update(_audits)
            .where(_audits.c.id <= upto_id)
            .values(created_at=now_utc() - timedelta(seconds=seconds))
        )


def _insert_manual_seal(engine, *, seq, from_id, to_id, pairs, row_count, prev_mac, kind="seal"):
    """Insert a hand-built (still key-signed) seal — for simulating a late commit into a range."""
    sealed_at = now_utc()
    rmac = integrity.rows_mac(_KEY, pairs)
    smac = integrity.seal_mac(
        _KEY,
        seq=seq,
        kind=kind,
        from_id=from_id,
        to_id=to_id,
        row_count=row_count,
        rows_mac=rmac,
        prev_mac=prev_mac,
        sealed_at=sealed_at,
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
            )
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
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, anchor_path=str(anchor)
    )
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
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, anchor_path=str(anchor)
    )
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


def test_truncating_an_anchored_seal_above_a_checkpoint_is_tampered(
    db_url: str, tmp_path, at_time
) -> None:
    # Adversarial finding (HIGH): once a checkpoint exists, the anchor's legitimacy test compared
    # the anchored seal *seq* to the checkpoint *floor* — but the floor is a row *id*, not a seq.
    # With 20 pruned rows the floor is 20 while the anchored head seal is seq 5, so ``seq <= floor``
    # was true and a genuine tail truncation of seq 5 was laundered to OK. The fix judges legitimacy
    # in seq-space (``seq <= head_seq`` with a checkpoint present), so the truncation is TAMPERED.
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, anchor_path=str(anchor)
    )
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(20):
                audit.record(f"old{i}")
        audit.sealer.run_once()  # seq 1 covers ids (0, 20] — all expired
        for i in range(3):
            audit.record(f"mid{i}")
        audit.sealer.run_once()  # seq 2 (20, 23] — recent, survives the prune
        for i in range(3):
            audit.record(f"mid2{i}")
        audit.sealer.run_once()  # seq 3 (23, 26]
        audit.retention.run_once()  # checkpoint seq 4 prunes seq 1 → floor = 20 (a row id)
        for i in range(3):
            audit.record(f"new{i}")
        audit.sealer.run_once()  # seq 5 (26, 29], exported to the anchor; seq 5 <= floor 20
        head_seq = max(s.seq for s in _seals_by_seq(audit.engine))
        assert audit.verify(anchor_path=str(anchor), full=True).outcome == "ok"  # baseline

        # Attacker truncates the anchored head seal (its rows stay). The surviving chain is dense
        # (…, checkpoint seq 4, no seq 5), but the anchor still names the vanished seq 5.
        with transaction(audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.seq == head_seq))
        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
        assert any("tail truncation" in f.message for f in report.findings)
    finally:
        audit.close()


def test_forged_row_below_the_checkpoint_floor_is_tampered(db_url: str, at_time) -> None:
    # Adversarial finding (HIGH): verify skips everything at/below the checkpoint floor because
    # retention deleted it, but never checked that the pruned region is actually empty. An attacker
    # inserts a fabricated row at an id <= floor — a range the checkpoint asserts holds zero rows —
    # and it was invisible even to ``--full`` while ``history()`` returned it. The bounded
    # pruned-region-empty probe now flags it, on every run (not just ``--full``).
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(5):
                audit.record(f"old{i}")
        audit.sealer.run_once()  # seq 1 (0, 5] — expired
        for i in range(3):
            audit.record(f"new{i}")
        audit.sealer.run_once()  # seq 2 (5, 8]
        audit.retention.run_once()  # checkpoint prunes (0, 5] → floor = 5, rows 1..5 deleted
        assert audit.verify(full=True).outcome == "ok"  # baseline: pruned region legitimately empty

        # Forge a row at id 3, inside the pruned (0, 5] region.
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(id=3, action="forged", created_at=now_utc()))
        assert any(r["action"] == "forged" for r in audit.history())  # history() returns it

        # Detected without --full (cheap, always-on) and with --full.
        rolling = audit.verify(full=False)
        assert rolling.outcome == "tampered"
        assert rolling.exit_code == 1
        assert any("pruned range" in f.message for f in rolling.findings)
        assert audit.verify(full=True).outcome == "tampered"
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
                conn.execute(
                    _audits.insert().values(
                        action=f"unsealed{i}", created_at=now_utc() - timedelta(seconds=7200)
                    )
                )

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
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, on_error=seen.append
    )
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
            conn.execute(
                update(_audits)
                .where(_audits.c.action == "range1.a")
                .values(action="range1.a.TAMPERED")
            )

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
            "range1.a.TAMPERED",
            "range1.b",
            "range2.a",
        }

        # And the evidence is not laundered: a subsequent verify still surfaces the tampering.
        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_prune_allows_a_valid_mac_late_commit(db_url: str, at_time) -> None:
    # A transaction that outran `grace` committed a genuine, validly-signed row into an already-
    # sealed range — verify's amber late-commit WARNING, NOT tampering (design 1A). On a real-
    # concurrency backend a writer racing the sealer's grace window makes this happen for real, so
    # retention must PRUNE such a range (aligned with verify's classification), not refuse it
    # forever. The late row is expired too, so deleting it with the range destroys no evidence.
    audit = AuditLog(database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            audit.record("first")  # id 1
            audit.record("late")  # id 2 — modelled as committing AFTER the range was sealed
            audit.record("third")  # id 3
        rows = _rows(audit.engine)
        # A valid-MAC seal over (0, 3] that counted only rows 1 and 3 — row 2 "committed late".
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

        deleted = audit.retention.run_once()
        assert deleted == 3  # the whole range pruned, the late row included
        assert audit.retention.last_refused_tampered == 0  # a late commit is not tampering
        assert _rows(audit.engine) == []  # no rows survive
        # The checkpoint advanced past the late-commit range (it did not stop at it).
        checkpoint = next(s for s in _seals_by_seq(audit.engine) if s.kind == "checkpoint")
        assert checkpoint.to_id == rows[2].id
        # And verify is clean across the checkpoint — the late commit was pruned, never laundered.
        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
    finally:
        audit.close()


def test_prune_refuses_an_invalid_extra_row(db_url: str) -> None:
    # Contrast with the late-commit case: an extra row that is NOT validly signed — a NULL-MAC
    # forged insert reusing a rollback-gap id inside a sealed range — makes the range diverge
    # without every present row being valid. That is TAMPERED, so retention must REFUSE it, never
    # treat it as a benign latecomer (the distinction that keeps a forged insert from being pruned).
    seen: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, on_error=seen.append
    )
    try:
        # Four expired rows with a rollback gap at id 3, sealed as a clean range (0, 5], boundary 5.
        with transaction(audit.engine) as conn:
            for rid, act in [(1, "L1"), (2, "L2"), (4, "L4"), (5, "L5")]:
                conn.execute(
                    _audits.insert().values(
                        id=rid, action=act, created_at=now_utc() - timedelta(seconds=7200)
                    )
                )
        assert audit.sealer.run_once() == 4  # seq 1 covers (0, 5], row_count 4
        # A forged NULL-MAC row slips into the gap (id 3) — an extra, invalid row in the range.
        with transaction(audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    id=3, action="FORGED", created_at=now_utc() - timedelta(seconds=7200)
                )
            )

        deleted = audit.retention.run_once()
        assert deleted == 0  # refused — nothing pruned
        assert audit.retention.last_refused_tampered == 1
        assert seen and "REFUSED" in str(seen[0])
        assert len(_rows(audit.engine)) == 5  # evidence preserved, not laundered by deletion
    finally:
        audit.close()


def test_prune_refuses_a_created_at_mutated_row(db_url: str) -> None:
    # The explicit post-insert `created_at` mutation case: the old aging trick is itself a
    # MAC-invalidating edit (created_at is bound into row_mac). Retention must REFUSE such a range,
    # not prune it — which is exactly why the aging convention moved to signed past-dated inserts.
    seen: list[BaseException] = []
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, on_error=seen.append
    )
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
    audit = AuditLog(
        database_url=db_url, mac_key=_SECRET, grace=0.0, max_age=3600.0, on_error=seen.append
    )
    try:
        audit.record("sealed")
        audit.sealer.run_once()
        with transaction(audit.engine) as conn:
            for i in range(3):  # 3 > threshold 2
                conn.execute(
                    _audits.insert().values(
                        action=f"unsealed{i}", created_at=now_utc() - timedelta(seconds=7200)
                    )
                )
        audit.retention.run_once()
        assert seen and "UNSEALED" in str(seen[0])
    finally:
        audit.close()
