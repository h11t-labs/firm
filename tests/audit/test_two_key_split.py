"""The two-key split (writer/row key vs seal key).

A distinct ``FIRM_AUDIT_SEAL_KEY`` signs everything on the seal side (``rows_mac`` + ``seal_mac``)
while the row key stays on every instance. The point of the feature is the security property: an
attacker who compromises an app instance holds only the row key and cannot forge the seal chain,
even after recomputing every MAC they *can* under the row key. Single-key mode (no seal key, or a
seal key equal to the row key) must stay byte-identical to before — the existing suite is the main
proof; the regression cases here are explicit.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema
from firm.audit.integrity import load_key, row_mac, rows_mac, seal_mac
from firm.audit.verify import VerifyError

_ROW_SECRET = "row-key-secret-padding-0123456789abcdef"  # noqa: S105  (>= 32 chars, throwaway)
_SEAL_SECRET = "seal-key-secret-padding-0123456789abcd"  # noqa: S105  (>= 32 chars, throwaway)
_ROW = load_key(_ROW_SECRET)
_SEAL = load_key(_SEAL_SECRET)
assert _ROW is not None and _SEAL is not None and _ROW.id != _SEAL.id

_audits = schema.audit_events
_seals = schema.seals


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FIRM_AUDIT_KEY",
        "FIRM_AUDIT_SEAL_KEY",
        "FIRM_AUDIT_KEYS",
        "FIRM_AUDIT_ANCHOR_PATH",
        "FIRM_AUDIT_VERIFY_STATE",
    ):
        monkeypatch.delenv(var, raising=False)


# -- helpers ------------------------------------------------------------------------------------


def _split(db_url: str, **kw) -> AuditLog:
    """A split-mode log: rows signed by the row key, seals by a distinct seal key. One instance can
    stand in for the writer, the sealer, and the verifier here (record uses the row key, the sealer
    uses the seal key, and verify holds both) — the deployment splits those roles across hosts, the
    crypto does not care which process runs each step."""
    return AuditLog(
        database_url=db_url, mac_key=_ROW_SECRET, seal_key=_SEAL_SECRET, grace=0.0, **kw
    )


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _seal_all(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.seq)).all()


def _row_mac_for(key, row, *, action: str) -> str:
    """Recompute a row's MAC under ``key`` with a changed ``action`` — the attacker's move once they
    hold the row key and want a content edit to keep recomputing."""
    return row_mac(
        key,
        entry_id=row.entry_id,
        action=action,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_label=row.subject_label,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        actor_label=row.actor_label,
        correlation_id=row.correlation_id,
        data=row.data,
        changes=row.changes,
        context=row.context,
        created_at=row.created_at,
    )


# -- key resolution: rows get the row key, seals get the seal key -------------------------------


def test_split_mode_e2e_signs_each_layer_with_its_own_key_and_verifies(db_url: str) -> None:
    audit = _split(db_url)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()

        # Rows carry the row key's id; the seal carries the seal key's id — no schema change, the
        # existing per-object key_id column records which key signed what (design point 2).
        assert {r.key_id for r in _rows(audit.engine)} == {_ROW.id}
        assert {s.key_id for s in _seal_all(audit.engine)} == {_SEAL.id}

        report = audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
        assert report.ok_count == 3
    finally:
        audit.close()


def test_verifier_keyrings_split_the_row_key_out_of_the_seal_ring(db_url: str) -> None:
    audit = _split(db_url)
    try:
        # The row keyring holds both labelled keys (design point 4); the seal keyring drops the row
        # key so a compromised instance's key can never validate a seal.
        assert set(audit.verifier.keyring) == {_ROW.id, _SEAL.id}
        assert set(audit.verifier.seal_keyring) == {_SEAL.id}
    finally:
        audit.close()


# -- the security property: the row key cannot forge the seal chain -----------------------------


def test_row_key_holder_cannot_rewrite_sealed_history(db_url: str) -> None:
    """The feature's reason to exist. An attacker with ONLY the row key edits a sealed row, then —
    doing everything they can under that key — recomputes the row's ``row_mac`` AND the seal's
    ``rows_mac``/``seal_mac`` with the row key. Verify still reports TAMPERED, because the seal is
    resolved and checked under the seal key (which the attacker does not have)."""
    audit = _split(db_url)
    try:
        for i in range(3):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        seal = _seal_all(audit.engine)[0]
        victim = _rows(audit.engine)[1]

        # Everything below uses only the row key — the blast radius of an instance compromise.
        forged_row_mac = _row_mac_for(_ROW, victim, action="HACKED")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.id == victim.id)
                .values(action="HACKED", row_mac=forged_row_mac)
            )
            pairs = [
                (r.id, r.row_mac)
                for r in conn.execute(
                    select(_audits)
                    .where(_audits.c.id > seal.from_id, _audits.c.id <= seal.to_id)
                    .order_by(_audits.c.id)
                ).all()
            ]
            # Re-seal the range under the row key — the attacker's best effort. key_id is left
            # naming the seal key; they cannot produce a seal-key MAC to go with a relabel.
            conn.execute(
                update(_seals)
                .where(_seals.c.seq == seal.seq)
                .values(
                    rows_mac=rows_mac(_ROW, pairs),
                    seal_mac=seal_mac(
                        _ROW,
                        seq=seal.seq,
                        kind=seal.kind,
                        from_id=seal.from_id,
                        to_id=seal.to_id,
                        row_count=seal.row_count,
                        rows_mac=rows_mac(_ROW, pairs),
                        prev_mac=seal.prev_mac,
                        sealed_at=seal.sealed_at,
                    ),
                )
            )

        report = audit.verify(full=True)
        assert report.outcome == "tampered"
        assert report.exit_code == 1
    finally:
        audit.close()


def test_row_key_holder_relabeling_the_seal_is_unverifiable_not_ok(db_url: str) -> None:
    """The same attack, but the attacker also relabels the seal's ``key_id`` to the row key's own id
    (the one key they possess). On a split verifier the row key is not a seal signer, so the seal's
    key_id is unknown-as-a-seal — a hard VerifyError with the two-key hint, never a laundered OK."""
    audit = _split(db_url)
    try:
        for i in range(2):
            audit.record(f"e{i}")
        audit.sealer.run_once()
        seal = _seal_all(audit.engine)[0]
        with transaction(audit.engine) as conn:
            pairs = [
                (r.id, r.row_mac)
                for r in conn.execute(select(_audits).order_by(_audits.c.id)).all()
            ]
            forged_rows_mac = rows_mac(_ROW, pairs)
            conn.execute(
                update(_seals)
                .where(_seals.c.seq == seal.seq)
                .values(
                    rows_mac=forged_rows_mac,
                    key_id=_ROW.id,
                    seal_mac=seal_mac(
                        _ROW,
                        seq=seal.seq,
                        kind=seal.kind,
                        from_id=seal.from_id,
                        to_id=seal.to_id,
                        row_count=seal.row_count,
                        rows_mac=forged_rows_mac,
                        prev_mac=seal.prev_mac,
                        sealed_at=seal.sealed_at,
                    ),
                )
            )

        with pytest.raises(VerifyError, match=r"not a seal key|two-key deployment"):
            audit.verify(full=True)
    finally:
        audit.close()


# -- retention in split mode --------------------------------------------------------------------


def test_checkpoint_prune_in_split_mode_signs_with_the_seal_key(db_url: str, at_time) -> None:
    audit = _split(db_url, max_age=3600.0)
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(3):
                audit.record(f"old{i}")
        audit.sealer.run_once()  # seq 1 (expired)
        for i in range(3):
            audit.record(f"new{i}")
        audit.sealer.run_once()  # seq 2 (fresh)

        deleted = audit.retention.run_once()
        assert deleted == 3
        assert audit.retention.last_refused_no_seal_key is False
        assert [r.action for r in _rows(audit.engine)] == ["new0", "new1", "new2"]
        checkpoint = next(s for s in _seal_all(audit.engine) if s.kind == "checkpoint")
        assert checkpoint.key_id == _SEAL.id  # the checkpoint is a seal — signed by the seal key

        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_retention_without_the_seal_key_refuses_loudly(db_url: str, at_time) -> None:
    """A two-key chain, pruned from a host that has only the row key. Writing a checkpoint would
    sign it with the wrong key, so retention refuses the whole aligned prune, routes it through
    on_error, and leaves the table untouched — pruning must run on a sealer-role host."""
    sealer = _split(db_url)  # owns the engine; has the seal key
    try:
        with at_time(now_utc() - timedelta(seconds=7200)):
            for i in range(3):
                sealer.record(f"old{i}")
        sealer.sealer.run_once()  # seal chain signed by the seal key

        seen: list[BaseException] = []
        # A non-sealer host: same DB, the row key only, no seal key configured.
        pruner = AuditLog(
            engine=sealer.engine,
            create_schema=False,
            mac_key=_ROW_SECRET,
            grace=0.0,
            max_age=3600.0,
            on_error=seen.append,
        )
        try:
            deleted = pruner.retention.run_once()
            assert deleted == 0  # refused — nothing pruned
            assert pruner.retention.last_refused_no_seal_key is True
            assert seen and "REFUSED" in str(seen[0]) and "seal key" in str(seen[0])
        finally:
            pruner.close()

        # Nothing was deleted and no checkpoint was written; the sealer host still verifies OK.
        assert [r.action for r in _rows(sealer.engine)] == ["old0", "old1", "old2"]
        assert all(s.kind == "seal" for s in _seal_all(sealer.engine))
        assert sealer.verify(full=True).outcome == "ok"
    finally:
        sealer.close()


# -- single-key mode stays byte-identical -------------------------------------------------------


def test_seal_key_equal_to_row_key_is_single_key_mode(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_ROW_SECRET, seal_key=_ROW_SECRET, grace=0.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        # Same key everywhere: the seal carries the row key's id and the seal keyring is the full
        # keyring (no split narrowing) — exactly the pre-split single-key behavior (design point 3).
        assert _seal_all(audit.engine)[0].key_id == _ROW.id
        assert set(audit.verifier.seal_keyring) == set(audit.verifier.keyring) == {_ROW.id}
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_no_seal_key_seals_with_the_row_key(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, mac_key=_ROW_SECRET, grace=0.0)
    try:
        audit.record("a")
        audit.sealer.run_once()
        assert _seal_all(audit.engine)[0].key_id == _ROW.id
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()
