"""Deterministic mutation x lifecycle matrix for the independent-seal design."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, integrity, schema
from firm.audit.verify import IntegrityAlert, VerifyError

_SINGLE_KEY = "attack-single-key-current-0123456789abcdef"
_ROW_KEY = "attack-row-key-current-0123456789abcdefghi"
_SEAL_KEY = "attack-seal-key-current-0123456789abcdefgh"
_OLD_ROW_KEY = "attack-row-key-retired-0123456789abcdefgh"
_OLD_SEAL_KEY = "attack-seal-key-retired-0123456789abcdefg"
_NEW_ROW_KEY = "attack-row-key-rotated-0123456789abcdefgh"
_NEW_SEAL_KEY = "attack-seal-key-rotated-0123456789abcdefg"
_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status


@dataclass(frozen=True)
class Lifecycle:
    stage: str
    anchor: bool
    keys: str

    @property
    def id(self) -> str:
        return f"{self.stage}-{'anchor' if self.anchor else 'no-anchor'}-{self.keys}"


@dataclass
class BuiltLog:
    audit: AuditLog
    anchor_path: Path | None
    target_row_ids: tuple[int, ...]
    target_seal_ids: tuple[int, ...]


_STAGES = ("fresh", "sealed", "sealed-tail", "retained-1", "retained-3")
_KEY_MODES = ("single", "split", "rotated")
_LIFECYCLES = tuple(
    Lifecycle(stage, anchor, keys)
    for stage in _STAGES
    for anchor in (False, True)
    for keys in _KEY_MODES
)
_ROW_MUTATIONS = ("edit-row", "delete-row", "insert-row", "relocate-row")
_SEAL_FIELD_MUTATIONS = (
    "edit-seal-kind",
    "edit-seal-from-id",
    "edit-seal-to-id",
    "edit-seal-row-count",
    "edit-seal-rows-mac",
    "edit-seal-seal-mac",
    "edit-seal-sealed-at",
    "edit-seal-key-id",
)
_SEAL_MUTATIONS = (*_SEAL_FIELD_MUTATIONS, "delete-seal", "swap-seals", "duplicate-seal")
_FLOOR_MUTATIONS = (
    "forge-floor-without-anchor",
    "over-advance-floor",
    "delete-newest-floor",
    "non-monotonic-floor",
)
_ACTIVATION_MUTATIONS = ("edit-activation", "delete-activation", "forge-activation")
_ANCHOR_MUTATIONS = ("edit-anchor", "truncate-anchor", "corrupt-anchor-line", "delete-anchor")


def _mutation_applies(lifecycle: Lifecycle, mutation: str) -> bool:
    if mutation in {
        "edit-row",
        "insert-row",
        "drop-seals-table",
        "edit-activation",
        "forge-activation",
    }:
        return True
    if mutation in _ROW_MUTATIONS or mutation in _SEAL_MUTATIONS:
        return lifecycle.stage != "fresh"
    if mutation in _FLOOR_MUTATIONS:
        return lifecycle.stage in {"retained-1", "retained-3"}
    if mutation == "delete-activation":
        return lifecycle.stage != "fresh" or lifecycle.anchor
    if mutation in _ANCHOR_MUTATIONS:
        return lifecycle.anchor
    raise AssertionError(mutation)


def _attack_params() -> list[pytest.ParameterSet]:
    mutations = (
        *_ROW_MUTATIONS,
        *_SEAL_MUTATIONS,
        *_FLOOR_MUTATIONS,
        *_ACTIVATION_MUTATIONS,
        *_ANCHOR_MUTATIONS,
        "drop-seals-table",
    )
    return [
        pytest.param(
            lifecycle,
            mutation,
            _expected_outcome(lifecycle, mutation),
            id=f"{lifecycle.id}-{mutation}",
        )
        for lifecycle in _LIFECYCLES
        for mutation in mutations
        if _mutation_applies(lifecycle, mutation)
    ]


def _expected_outcome(lifecycle: Lifecycle, mutation: str) -> str:
    if lifecycle.stage == "fresh" and mutation in {"delete-anchor", "truncate-anchor"}:
        return "warning"
    return "tampered"


_ATTACK_PARAMS = _attack_params()


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


def _key_pair(mode: str, *, rotated: bool = False) -> tuple[str, str]:
    if mode == "single":
        return _SINGLE_KEY, _SINGLE_KEY
    if mode == "split":
        return _ROW_KEY, _SEAL_KEY
    if rotated:
        return _NEW_ROW_KEY, _NEW_SEAL_KEY
    return _OLD_ROW_KEY, _OLD_SEAL_KEY


def _open_log(
    database_url: str,
    lifecycle: Lifecycle,
    anchor_path: Path | None,
    *,
    rotated: bool = False,
    create_schema: bool = True,
) -> AuditLog:
    row_key, seal_key = _key_pair(lifecycle.keys, rotated=rotated)
    return AuditLog(
        database_url=database_url,
        create_schema=create_schema,
        mac_key=row_key,
        seal_key=seal_key,
        max_age=3600.0,
        grace=0.0,
        seal_batch_size=2,
        anchor_path=str(anchor_path) if anchor_path is not None else None,
        anchor_max_age=3600.0,
        unsealed_tail_max_age=3600.0,
        on_error=lambda _error: None,
        on_finding=lambda _finding: None,
    )


def _records(audit: AuditLog) -> list:
    with transaction(audit.engine) as conn:
        return conn.execute(select(_seals).order_by(_seals.c.id)).all()


def _range_seals(audit: AuditLog) -> list:
    return [record for record in _records(audit) if record.kind == "seal"]


def _record_old(audit: AuditLog, prefix: str, count: int) -> tuple[int, ...]:
    before = _max_row_id(audit)
    old = now_utc() - timedelta(hours=2)
    with patch("firm.audit.events.now_utc", lambda: old):
        for index in range(count):
            audit.record(f"{prefix}.{index}")
    with transaction(audit.engine) as conn:
        return tuple(
            conn.execute(
                select(_audits.c.id).where(_audits.c.id > before).order_by(_audits.c.id)
            ).scalars()
        )


def _seal_old(audit: AuditLog) -> tuple[int, ...]:
    before = {record.id for record in _range_seals(audit)}
    old = now_utc() - timedelta(hours=2)
    with patch("firm.audit.sealing.now_utc", lambda: old):
        assert audit.sealer.run_once() > 0
    return tuple(record.id for record in _range_seals(audit) if record.id not in before)


def _max_row_id(audit: AuditLog) -> int:
    with transaction(audit.engine) as conn:
        return conn.execute(select(func.max(_audits.c.id))).scalar_one() or 0


def _build_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle) -> BuiltLog:
    database_url = f"sqlite:///{tmp_path / 'attack.db'}"
    anchor_path = tmp_path / "anchor.log" if lifecycle.anchor else None
    audit = _open_log(database_url, lifecycle, anchor_path)
    assert audit.sealer.run_once() == 0  # signed activation at boundary zero

    retention_count = {"retained-1": 1, "retained-3": 3}.get(lifecycle.stage, 0)
    for cycle in range(retention_count):
        _record_old(audit, f"retired-{cycle}", 2)
        _seal_old(audit)
        assert audit.retention.run_once() == 2

    target_ids = _record_old(audit, "target", 4)
    target_seal_ids: tuple[int, ...] = ()
    if lifecycle.stage != "fresh":
        target_seal_ids = _seal_old(audit)
        assert len(target_seal_ids) == 2

    if lifecycle.keys == "rotated":
        audit.close()
        monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old-row={_OLD_ROW_KEY}")
        monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old-seal={_OLD_SEAL_KEY}")
        audit = _open_log(database_url, lifecycle, anchor_path, rotated=True, create_schema=False)

    if lifecycle.stage == "sealed-tail":
        audit.record("young-tail")

    if lifecycle.stage == "fresh":
        target_range_ids = target_ids[:2]
    else:
        target = next(record for record in _range_seals(audit) if record.id == target_seal_ids[0])
        target_range_ids = tuple(row_id for row_id in target_ids if row_id <= target.to_id)
    return BuiltLog(audit, anchor_path, target_range_ids, target_seal_ids)


def _target_seal(built: BuiltLog):
    return next(
        record for record in _range_seals(built.audit) if record.id == built.target_seal_ids[0]
    )


def _newest_floor(audit: AuditLog):
    floors = [record for record in _records(audit) if record.kind == "floor"]
    assert floors
    return floors[-1]


def _mutate_row(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    victim = built.target_row_ids[0]
    if mutation == "edit-row":
        with transaction(built.audit.engine) as conn:
            conn.execute(update(_audits).where(_audits.c.id == victim).values(action="FORGED"))
        return built.target_row_ids
    if mutation == "delete-row":
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
        return built.target_row_ids
    if mutation == "insert-row":
        inserted = _max_row_id(built.audit) + 1
        with transaction(built.audit.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    id=inserted,
                    action="FORGED",
                    created_at=now_utc() - timedelta(hours=2),
                )
            )
        return (inserted,)
    relocated = _max_row_id(built.audit) + 100
    with transaction(built.audit.engine) as conn:
        conn.execute(update(_audits).where(_audits.c.id == victim).values(id=relocated))
    return built.target_row_ids


def _mutate_seal_field(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    target = _target_seal(built)
    values = {
        "edit-seal-kind": {"kind": "forged"},
        "edit-seal-from-id": {"from_id": target.from_id + 1},
        "edit-seal-to-id": {"to_id": target.to_id - 1},
        "edit-seal-row-count": {"row_count": target.row_count + 1},
        "edit-seal-rows-mac": {"rows_mac": "0" * 64},
        "edit-seal-seal-mac": {"seal_mac": "0" * 64},
        "edit-seal-sealed-at": {"sealed_at": target.sealed_at + timedelta(seconds=1)},
        "edit-seal-key-id": {"key_id": "deadbeef"},
    }
    with transaction(built.audit.engine) as conn:
        conn.execute(update(_seals).where(_seals.c.id == target.id).values(**values[mutation]))
    return built.target_row_ids


def _mutate_seals(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    target = _target_seal(built)
    if mutation == "delete-seal":
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.id == target.id))
    elif mutation == "swap-seals":
        first, second = [
            record for record in _range_seals(built.audit) if record.id in built.target_seal_ids[:2]
        ]
        with transaction(built.audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.id == first.id).values(from_id=-999_999))
            conn.execute(
                update(_seals).where(_seals.c.id == second.id).values(from_id=first.from_id)
            )
            conn.execute(
                update(_seals).where(_seals.c.id == first.id).values(from_id=second.from_id)
            )
    elif mutation == "duplicate-seal":
        payload = {
            column.name: getattr(target, column.name) for column in _seals.c if column.name != "id"
        }
        with transaction(built.audit.engine) as conn:
            conn.exec_driver_sql("DROP INDEX index_firm_audit_seals_on_from_id")
            conn.execute(_seals.insert().values(**payload))
    else:
        raise AssertionError(mutation)
    return built.target_row_ids


def _mutate_floor(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    floor = _newest_floor(built.audit)
    if mutation == "forge-floor-without-anchor":
        with transaction(built.audit.engine) as conn:
            conn.execute(
                _seals.insert().values(
                    kind="floor",
                    from_id=None,
                    to_id=floor.to_id + 1,
                    seal_mac="f" * 64,
                    sealed_at=now_utc(),
                    key_id=floor.key_id,
                )
            )
    elif mutation == "over-advance-floor":
        with transaction(built.audit.engine) as conn:
            conn.execute(
                update(_seals)
                .where(_seals.c.id == floor.id)
                .values(to_id=_max_row_id(built.audit) + 100)
            )
    elif mutation == "delete-newest-floor":
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.id == floor.id))
    elif mutation == "non-monotonic-floor":
        payload = {
            column.name: getattr(floor, column.name) for column in _seals.c if column.name != "id"
        }
        with transaction(built.audit.engine) as conn:
            conn.execute(_seals.insert().values(**payload))
        if built.anchor_path is not None:
            parts = [
                line
                for line in built.anchor_path.read_text(encoding="utf-8").splitlines()
                if " FLOOR " in line
            ][-1]
            with built.anchor_path.open("a", encoding="utf-8") as handle:
                handle.write(parts + "\n")
    else:
        raise AssertionError(mutation)
    return built.target_row_ids


def _mutate_activation(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    activation = next(record for record in _records(built.audit) if record.kind == "activation")
    if mutation == "edit-activation":
        with transaction(built.audit.engine) as conn:
            conn.execute(update(_seals).where(_seals.c.id == activation.id).values(to_id=1))
    elif mutation == "delete-activation":
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.id == activation.id))
    elif mutation == "forge-activation":
        with transaction(built.audit.engine) as conn:
            conn.execute(
                _seals.insert().values(
                    kind="activation",
                    from_id=-2,
                    to_id=activation.to_id,
                    seal_mac="f" * 64,
                    sealed_at=now_utc(),
                    key_id=activation.key_id,
                )
            )
    else:
        raise AssertionError(mutation)
    return built.target_row_ids


def _mutate_anchor(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    path = built.anchor_path
    assert path is not None and path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    if mutation == "edit-anchor":
        parts = lines[-1].split()
        parts[-1] = "0" * 64
        lines[-1] = " ".join(parts)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif mutation == "truncate-anchor":
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    elif mutation == "corrupt-anchor-line":
        lines.insert(0, "corrupt anchor line")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif mutation == "delete-anchor":
        path.unlink()
    return ()


def _apply_mutation(built: BuiltLog, mutation: str) -> tuple[int, ...]:
    if mutation in _ROW_MUTATIONS:
        return _mutate_row(built, mutation)
    if mutation in _SEAL_FIELD_MUTATIONS:
        return _mutate_seal_field(built, mutation)
    if mutation in {"delete-seal", "swap-seals", "duplicate-seal"}:
        return _mutate_seals(built, mutation)
    if mutation in _FLOOR_MUTATIONS:
        return _mutate_floor(built, mutation)
    if mutation in _ACTIVATION_MUTATIONS:
        return _mutate_activation(built, mutation)
    if mutation in _ANCHOR_MUTATIONS:
        return _mutate_anchor(built, mutation)
    with transaction(built.audit.engine) as conn:
        conn.exec_driver_sql("DROP TABLE firm_audit_seals")
    return ()


def _status_outcome(audit: AuditLog) -> str | None:
    with transaction(audit.engine) as conn:
        row = conn.execute(select(_status.c.outcome).where(_status.c.id == 1)).first()
    return row.outcome if row is not None else None


def _assert_verify_outcome(built: BuiltLog, expected: str) -> None:
    if expected == "error":
        with pytest.raises(VerifyError):
            built.audit.verify(full=True)
        assert _status_outcome(built.audit) == "error"
        return
    report = built.audit.verify(full=True)
    assert report.outcome == expected


def _present_ids(audit: AuditLog, wanted: tuple[int, ...]) -> set[int]:
    if not wanted:
        return set()
    with transaction(audit.engine) as conn:
        return set(conn.execute(select(_audits.c.id).where(_audits.c.id.in_(wanted))).scalars())


def _assert_retention_does_not_launder(
    built: BuiltLog, protected_ids: tuple[int, ...], mutation: str
) -> None:
    if (
        not protected_ids
        or not built.target_seal_ids
        or mutation in _ANCHOR_MUTATIONS
        or mutation == "drop-seals-table"
    ):
        return
    before = _present_ids(built.audit, protected_ids)
    built.audit.retention.run_once()
    after = _present_ids(built.audit, tuple(before))
    assert built.audit.retention.last_refused_tampered > 0 or after == before


@pytest.mark.parametrize("lifecycle", _LIFECYCLES, ids=lambda lifecycle: lifecycle.id)
def test_clean_lifecycle_positive_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle
) -> None:
    built = _build_log(tmp_path, monkeypatch, lifecycle)
    try:
        expected_clean = "warning" if lifecycle.stage == "fresh" else "ok"
        assert built.audit.verify(full=True).outcome == expected_clean
        deleted = built.audit.retention.run_once()
        if lifecycle.stage == "fresh":
            assert deleted == 0
            assert built.audit.retention.last_skipped_unsealed > 0
        else:
            assert deleted > 0
            assert built.audit.retention.last_refused_tampered == 0
            assert not _present_ids(built.audit, built.target_row_ids)
            assert built.audit.verify(full=True).outcome == "ok"
    finally:
        built.audit.close()


@pytest.mark.parametrize("lifecycle,mutation,expected", _ATTACK_PARAMS)
def test_attack_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: Lifecycle,
    mutation: str,
    expected: str,
) -> None:
    built = _build_log(tmp_path, monkeypatch, lifecycle)
    try:
        protected_ids = _apply_mutation(built, mutation)
        _assert_verify_outcome(built, expected)
        _assert_retention_does_not_launder(built, protected_ids, mutation)
    finally:
        built.audit.close()


def test_orphaned_floor_anchor_is_a_crashed_prune_and_next_retention_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        first_seal = _range_seals(built.audit)[0]
        retired_at = now_utc()
        seal_key = built.audit._seal_key
        assert seal_key is not None
        mac = integrity.floor_mac(
            seal_key,
            through_id=first_seal.to_id,
            retired_at=retired_at,
            key_id=seal_key.id,
        )
        assert built.audit.sealer._emit_anchor(
            kind="floor",
            from_id=None,
            to_id=first_seal.to_id,
            mac=mac,
            at=retired_at,
        )

        report = built.audit.verify(full=True)
        assert report.outcome == "warning"
        assert any("crashed prune" in finding.message for finding in report.findings)

        assert built.audit.retention.run_once() > 0
        assert built.audit.verify(full=True).outcome == "ok"
    finally:
        built.audit.close()


def test_sealer_heals_a_committed_seal_missing_from_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        built.audit._anchor_max_age = 24 * 60 * 60
        target = _target_seal(built)
        path = built.anchor_path
        assert path is not None
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if line.split()[-1] != target.seal_mac]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert built.audit.verify(full=True).outcome == "ok"
        assert built.audit.sealer.run_once() == 0
        healed = path.read_text(encoding="utf-8").splitlines()
        assert any(line.split()[-1] == target.seal_mac for line in healed)
        assert built.audit.verify(full=True).outcome == "ok"
    finally:
        built.audit.close()


def test_partial_anchor_tail_warns_heals_and_does_not_block_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        built.audit._anchor_max_age = 24 * 60 * 60
        path = built.anchor_path
        assert path is not None
        lines = path.read_text(encoding="utf-8").splitlines()
        complete = lines[-1]
        partial = complete[: complete.index(" SEAL ") + len(" SEA")]
        path.write_text("\n".join([*lines[:-1], partial]), encoding="utf-8")

        report = built.audit.verify(full=True)
        assert report.outcome == "warning"
        assert any("partial anchor append" in finding.message for finding in report.findings)

        assert built.audit.sealer.run_once() == 0
        assert partial in path.read_text(encoding="utf-8").splitlines()
        assert built.audit.retention.run_once() > 0
        assert built.audit.retention.last_refused_tampered == 0
        assert built.audit.verify(full=True).outcome == "warning"
    finally:
        built.audit.close()


def test_corrupted_non_final_anchor_line_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        _mutate_anchor(built, "corrupt-anchor-line")
        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_deleted_floor_and_anchor_line_cannot_hide_already_pruned_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("retained-1", True, "single"))
    try:
        floor = _newest_floor(built.audit)
        with transaction(built.audit.engine) as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(_audits).where(_audits.c.id <= floor.to_id)
                ).scalar_one()
                == 0
            )
            conn.execute(delete(_seals).where(_seals.c.id == floor.id))

        path = built.anchor_path
        assert path is not None
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if line.split()[-1] != floor.seal_mac]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_row_key_signed_layer_two_forgery_is_tampered_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", False, "split"))
    alerts: list[IntegrityAlert] = []
    built.audit.on_finding = alerts.append
    try:
        seal = _target_seal(built)
        row_key = integrity.load_key(_ROW_KEY)
        assert row_key is not None
        with transaction(built.audit.engine) as conn:
            rows = conn.execute(
                select(_audits.c.id, _audits.c.row_mac)
                .where(_audits.c.id > seal.from_id, _audits.c.id <= seal.to_id)
                .order_by(_audits.c.id)
            ).all()
            aggregate = integrity.rows_mac(row_key, [(row.id, row.row_mac) for row in rows])
            forged_mac = integrity.seal_mac(
                row_key,
                from_id=seal.from_id,
                to_id=seal.to_id,
                row_count=seal.row_count,
                rows_mac=aggregate,
                sealed_at=seal.sealed_at,
                key_id=row_key.id,
            )
            conn.execute(
                update(_seals)
                .where(_seals.c.id == seal.id)
                .values(key_id=row_key.id, rows_mac=aggregate, seal_mac=forged_mac)
            )
        report = built.audit.verify(full=True)
        assert report.outcome == "tampered"
        assert alerts and alerts[0].severity == "critical"
    finally:
        built.audit.close()


def test_real_row_tamper_plus_unknown_layer_two_key_is_tampered_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 regression guard: junk signer evidence cannot suppress the real tamper alert."""
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", False, "single"))
    alerts: list[IntegrityAlert] = []
    built.audit.on_finding = alerts.append
    try:
        with transaction(built.audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.id == built.target_row_ids[0])
                .values(action="HACKED")
            )
            conn.execute(
                _seals.insert().values(
                    kind="floor",
                    from_id=None,
                    to_id=0,
                    row_count=None,
                    rows_mac=None,
                    seal_mac="f" * 64,
                    sealed_at=now_utc(),
                    key_id="deadbeef",
                )
            )
        report = built.audit.verify(full=True)
        assert report.outcome == "tampered"
        assert alerts and alerts[0].severity == "critical"
    finally:
        built.audit.close()


def test_unknown_row_key_as_sole_obstacle_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("fresh", False, "single"))
    try:
        with transaction(built.audit.engine) as conn:
            conn.execute(
                update(_audits)
                .where(_audits.c.id == built.target_row_ids[0])
                .values(key_id="deadbeef")
            )
        _assert_verify_outcome(built, "error")
    finally:
        built.audit.close()


@pytest.mark.parametrize("kind", ["SEAL", "FLOOR"])
def test_replayed_valid_anchor_event_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("retained-1", True, "single"))
    try:
        path = built.anchor_path
        assert path is not None
        replay = next(
            line for line in path.read_text(encoding="utf-8").splitlines() if f" {kind} " in line
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(replay + "\n")
        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_retention_missing_retired_seal_key_refuses_without_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", False, "split"))
    before = _present_ids(built.audit, built.target_row_ids)
    database_url = str(built.audit.engine.url)
    built.audit.close()
    pruner = AuditLog(
        database_url=database_url,
        create_schema=False,
        mac_key=_ROW_KEY,
        seal_key=_NEW_SEAL_KEY,
        max_age=3600.0,
        on_error=lambda _error: None,
    )
    try:
        assert pruner.retention.run_once() == 0
        assert pruner.retention.last_refused_no_seal_key is True
        assert _present_ids(pruner, tuple(before)) == before
    finally:
        pruner.close()


def test_orphan_floor_past_resolved_floor_without_anchored_seals_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("retained-1", True, "single"))
    try:
        built.audit._anchor_max_age = 24 * 60 * 60
        floor = _newest_floor(built.audit)
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_seals).where(_seals.c.id == floor.id))
        path = built.anchor_path
        assert path is not None
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if " SEAL " not in line
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_null_row_mac_is_unprotected_at_boundary_but_tampered_in_tail(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'null-mac.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=0.0)
    try:
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="legacy", created_at=now_utc()))
        assert audit.sealer.run_once() == 0
        legacy = audit.verify(full=True)
        assert legacy.outcome == "ok"
        assert legacy.unprotected_count == 1

        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="tail", created_at=now_utc()))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_activation_boundary_uses_grace_cutoff_and_young_row_is_later_sealed(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'activation-grace.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=3600.0)
    try:
        old = now_utc() - timedelta(hours=2)
        with patch("firm.audit.events.now_utc", lambda: old):
            audit.record("old")
        audit.record("young")
        assert audit.sealer.run_once() == 0
        activation = next(record for record in _records(audit) if record.kind == "activation")
        assert activation.to_id == 1

        later = now_utc() + timedelta(hours=2)
        with patch("firm.audit.sealing.now_utc", lambda: later):
            assert audit.sealer.run_once() == 1
        assert _range_seals(audit)[-1].to_id == 2
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()
