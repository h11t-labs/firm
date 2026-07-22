"""Role-scoped row/seal keys across seals, markers, retention, and rotation."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema
from firm.audit.integrity import load_key, row_mac, rows_mac, seal_mac
from firm.audit.verify import VerifyError

_ROW_SECRET = "row-key-secret-padding-0123456789abcdef"  # noqa: S105
_SEAL_SECRET = "seal-key-secret-padding-0123456789abcd"  # noqa: S105
_ROW2_SECRET = "row-key-two-secret-padding-0123456789ab"  # noqa: S105
_SEAL2_SECRET = "seal-key-two-secret-padding-0123456789a"  # noqa: S105
_ROW = load_key(_ROW_SECRET)
_SEAL = load_key(_SEAL_SECRET)
_ROW2 = load_key(_ROW2_SECRET)
_SEAL2 = load_key(_SEAL2_SECRET)
assert _ROW is not None and _SEAL is not None and _ROW2 is not None and _SEAL2 is not None
_audits = schema.audit_events
_seals = schema.seals


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


def _split(db_url: str, *, activate: bool = True, **kwargs) -> AuditLog:
    audit = AuditLog(
        database_url=db_url,
        mac_key=_ROW_SECRET,
        seal_key=_SEAL_SECRET,
        grace=0.0,
        **kwargs,
    )
    if activate:
        assert audit.sealer.run_once() == 0
    return audit


def _rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _records(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.id)).all()


def _range_seal(engine):
    return next(record for record in _records(engine) if record.kind == "seal")


def _row_mac_for(key, row, action: str) -> str:
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


def test_split_mode_signs_rows_and_all_side_records_with_role_keys(db_url: str) -> None:
    audit = _split(db_url)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        assert {row.key_id for row in _rows(audit.engine)} == {_ROW.id}
        assert {record.key_id for record in _records(audit.engine)} == {_SEAL.id}
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_verifier_keyrings_are_role_scoped(db_url: str) -> None:
    audit = _split(db_url)
    try:
        assert set(audit.verifier.keyring) == {_ROW.id}
        assert set(audit.verifier.seal_keyring) == {_SEAL.id}
    finally:
        audit.close()


def test_row_key_holder_cannot_rewrite_sealed_history(db_url: str) -> None:
    audit = _split(db_url)
    try:
        for index in range(3):
            audit.record(f"e{index}")
        audit.sealer.run_once()
        seal = _range_seal(audit.engine)
        victim = _rows(audit.engine)[1]
        forged_row = _row_mac_for(_ROW, victim, "HACKED")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.id == victim.id)
                .values(action="HACKED", row_mac=forged_row)
            )
            current = conn.execute(
                select(_audits).where(_audits.c.id <= seal.to_id).order_by(_audits.c.id)
            ).all()
            aggregate = rows_mac(_ROW, [(row.id, row.row_mac) for row in current])
            conn.execute(
                update(_seals)
                .where(_seals.c.id == seal.id)
                .values(
                    rows_mac=aggregate,
                    seal_mac=seal_mac(
                        _ROW,
                        from_id=seal.from_id,
                        to_id=seal.to_id,
                        row_count=seal.row_count,
                        rows_mac=aggregate,
                        sealed_at=seal.sealed_at,
                        key_id=seal.key_id,
                    ),
                )
            )
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_row_key_relabeling_seal_is_tampered(db_url: str) -> None:
    audit = _split(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        seal = _range_seal(audit.engine)
        pairs = [(row.id, row.row_mac) for row in _rows(audit.engine)]
        aggregate = rows_mac(_ROW, pairs)
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_seals)
                .where(_seals.c.id == seal.id)
                .values(
                    key_id=_ROW.id,
                    rows_mac=aggregate,
                    seal_mac=seal_mac(
                        _ROW,
                        from_id=seal.from_id,
                        to_id=seal.to_id,
                        row_count=seal.row_count,
                        rows_mac=aggregate,
                        sealed_at=seal.sealed_at,
                        key_id=_ROW.id,
                    ),
                )
            )
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_floor_prune_in_split_mode_uses_seal_key(db_url: str, at_time) -> None:
    audit = _split(db_url, max_age=3600.0)
    try:
        old = now_utc() - timedelta(hours=2)
        with at_time(old):
            audit.record("old")
        with patch("firm.audit.sealing.now_utc", lambda: old):
            audit.sealer.run_once()
        assert audit.retention.run_once() == 1
        floor = next(record for record in _records(audit.engine) if record.kind == "floor")
        assert floor.key_id == _SEAL.id
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_retention_without_seal_key_refuses_loudly(db_url: str, at_time) -> None:
    owner = _split(db_url, max_age=3600.0)
    try:
        old = now_utc() - timedelta(hours=2)
        with at_time(old):
            owner.record("old")
        with patch("firm.audit.sealing.now_utc", lambda: old):
            owner.sealer.run_once()
        seen: list[BaseException] = []
        pruner = AuditLog(
            engine=owner.engine,
            create_schema=False,
            mac_key=_ROW_SECRET,
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


def test_retired_row_key_cannot_sign_seals(db_url: str, monkeypatch) -> None:
    audit = _split(db_url)
    try:
        audit.record("a")
        audit.sealer.run_once()
        seal = _range_seal(audit.engine)
        aggregate = rows_mac(_ROW, [(row.id, row.row_mac) for row in _rows(audit.engine)])
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_seals)
                .where(_seals.c.id == seal.id)
                .values(
                    key_id=_ROW.id,
                    rows_mac=aggregate,
                    seal_mac=seal_mac(
                        _ROW,
                        from_id=seal.from_id,
                        to_id=seal.to_id,
                        row_count=seal.row_count,
                        rows_mac=aggregate,
                        sealed_at=seal.sealed_at,
                        key_id=_ROW.id,
                    ),
                )
            )
    finally:
        audit.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"r1={_ROW_SECRET}")
    verifier = AuditLog(
        database_url=db_url,
        create_schema=False,
        mac_key=_ROW2_SECRET,
        seal_key=_SEAL_SECRET,
    )
    try:
        assert verifier.verify(full=True).outcome == "tampered"
    finally:
        verifier.close()


def test_split_row_key_rotation_verifies_old_rows(db_url: str, monkeypatch) -> None:
    original = _split(db_url)
    try:
        original.record("a")
        original.sealer.run_once()
    finally:
        original.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"r1={_ROW_SECRET}")
    verifier = AuditLog(
        database_url=db_url,
        create_schema=False,
        mac_key=_ROW2_SECRET,
        seal_key=_SEAL_SECRET,
    )
    try:
        assert verifier.verify(full=True).outcome == "ok"
    finally:
        verifier.close()


def test_split_seal_key_rotation_verifies_old_records(db_url: str, monkeypatch) -> None:
    original = _split(db_url)
    try:
        original.record("a")
        original.sealer.run_once()
    finally:
        original.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"s1={_SEAL_SECRET}")
    verifier = AuditLog(
        database_url=db_url,
        create_schema=False,
        mac_key=_ROW_SECRET,
        seal_key=_SEAL2_SECRET,
    )
    try:
        assert verifier.verify(full=True).outcome == "ok"
    finally:
        verifier.close()


def test_single_key_rotation_uses_retired_seal_archive(db_url: str, monkeypatch) -> None:
    original = AuditLog(database_url=db_url, mac_key=_ROW_SECRET, grace=0.0)
    try:
        original.sealer.run_once()
        original.record("a")
        original.sealer.run_once()
    finally:
        original.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old={_ROW_SECRET}")
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old={_ROW_SECRET}")
    verifier = AuditLog(database_url=db_url, create_schema=False, mac_key=_ROW2_SECRET)
    try:
        assert verifier.verify(full=True).outcome == "ok"
    finally:
        verifier.close()


def test_retired_seal_archive_does_not_authorize_old_row_macs(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = AuditLog(database_url=db_url, mac_key=_ROW_SECRET, grace=0.0)
    try:
        original.sealer.run_once()
        original.record("a")
        original.sealer.run_once()
    finally:
        original.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old={_ROW_SECRET}")
    verifier = AuditLog(database_url=db_url, create_schema=False, mac_key=_ROW2_SECRET)
    try:
        with pytest.raises(VerifyError, match="unknown key_id"):
            verifier.verify(full=True)
    finally:
        verifier.close()


def test_equal_or_missing_seal_key_is_single_key_mode(db_url: str) -> None:
    explicit = AuditLog(database_url=db_url, mac_key=_ROW_SECRET, seal_key=_ROW_SECRET, grace=0.0)
    try:
        explicit.sealer.run_once()
        assert explicit._seal_key_split is False
        assert set(explicit.verifier.seal_keyring) == {_ROW.id}
    finally:
        explicit.close()


def test_row_and_seal_key_id_collision_is_startup_error(db_url: str, monkeypatch) -> None:
    monkeypatch.setattr("firm.audit.integrity.key_id", lambda _secret: "deadbeef")
    with pytest.raises(ValueError, match="share key_id"):
        AuditLog(database_url=db_url, mac_key=_ROW_SECRET, seal_key=_SEAL_SECRET)


def test_verify_keyring_collision_via_retired_key_is_error(db_url: str, monkeypatch) -> None:
    monkeypatch.setattr("firm.audit.integrity.key_id", lambda _secret: "deadbeef")
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old={_SEAL_SECRET}")
    audit = AuditLog(database_url=db_url, mac_key=_ROW_SECRET)
    try:
        with pytest.raises(VerifyError, match="share key_id"):
            _ = audit.verifier.keyring
    finally:
        audit.close()
