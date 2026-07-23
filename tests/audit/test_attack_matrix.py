"""Deterministic mutation x lifecycle matrix for the independent-seal design."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, integrity, schema, verify
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
    if mutation == "delete-all-seal-records":
        return lifecycle.stage != "fresh" and not lifecycle.anchor
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
        "delete-all-seal-records",
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
    clean = "warning" if lifecycle.stage == "fresh" else "ok"
    if mutation in {"edit-anchor", "truncate-anchor"}:
        return clean
    if mutation in {"corrupt-anchor-line", "delete-anchor"}:
        return "warning"
    if mutation == "delete-activation" and lifecycle.stage == "fresh":
        return "ok"
    if mutation == "delete-newest-floor" and lifecycle.anchor:
        return clean
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
    if mutation == "delete-all-seal-records":
        victim = built.target_row_ids[0]
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == victim))
            conn.execute(delete(_seals))
        return built.target_row_ids
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
        or (mutation == "delete-newest-floor" and built.anchor_path is not None)
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
        if mutation == "delete-all-seal-records":
            assert built.audit.verify(full=True).outcome == "ok"
        protected_ids = _apply_mutation(built, mutation)
        _assert_verify_outcome(built, expected)
        _assert_retention_does_not_launder(built, protected_ids, mutation)
    finally:
        built.audit.close()


def test_orphaned_floor_watermark_marks_present_pruned_region_tampered(
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

        assert built.audit.verify(full=True).outcome == "tampered"
        assert built.audit.retention.run_once() == 0
        assert built.audit.retention.last_refused_tampered == 1
    finally:
        built.audit.close()


def test_sealer_heals_a_committed_seal_missing_from_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        built.audit._anchor_max_age = 24 * 60 * 60
        target = _range_seals(built.audit)[-1]
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
        assert any("unreadable line" in finding.message for finding in report.findings)

        assert built.audit.sealer.run_once() == 0
        assert partial in path.read_text(encoding="utf-8").splitlines()
        assert built.audit.retention.run_once() > 0
        assert built.audit.retention.last_refused_tampered == 0
        assert built.audit.verify(full=True).outcome == "warning"
    finally:
        built.audit.close()


def test_corrupted_non_final_anchor_line_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        _mutate_anchor(built, "corrupt-anchor-line")
        report = built.audit.verify(full=True)
        assert report.outcome == "warning"
        assert report.exit_code == 0
    finally:
        built.audit.close()


def test_append_heavy_anchor_cannot_launder_newest_seal_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("retained-1", True, "single"))
    try:
        floor = _newest_floor(built.audit)
        path = built.anchor_path
        assert path is not None
        at = now_utc().isoformat(timespec="microseconds")
        with path.open("a", encoding="utf-8") as handle:
            for _ in range(verify._MAX_FINDINGS + 50):
                handle.write(f"{at} SEAL 0 {floor.to_id} junk\n")
        newest = _range_seals(built.audit)[-1]
        with transaction(built.audit.engine) as conn:
            conn.execute(
                delete(_audits).where(_audits.c.id > newest.from_id, _audits.c.id <= newest.to_id)
            )
            conn.execute(delete(_seals).where(_seals.c.id == newest.id))

        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_verify_acquires_database_snapshot_before_reading_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    order: list[str] = []
    original_load = verify.load_seal_records
    original_read = verify._read_anchor

    def load_first(conn):
        order.append("database")
        return original_load(conn)

    def read_second(path, **kwargs):
        order.append("anchor")
        return original_read(path, **kwargs)

    try:
        with (
            patch("firm.audit.verify.load_seal_records", load_first),
            patch("firm.audit.verify._read_anchor", read_second),
        ):
            assert built.audit.verify(full=True).outcome == "ok"
        assert order[:2] == ["database", "anchor"]
    finally:
        built.audit.close()


def test_many_seal_floor_cycles_stay_ok_and_retention_keeps_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex-1 guard: anchor history length cannot deadlock or false-alarm retention."""
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        assert built.audit.verify(full=True).outcome == "ok"
        assert built.audit.retention.run_once() == 4
        for cycle in range(25):
            _record_old(built.audit, f"long-running-{cycle}", 2)
            _seal_old(built.audit)
            assert built.audit.verify(full=True).outcome == "ok"
            assert built.audit.retention.run_once() == 2
            assert built.audit.retention.last_refused_tampered == 0
        assert built.audit.verify(full=True).outcome == "ok"
    finally:
        built.audit.close()


def test_young_anchored_seal_invisible_to_database_snapshot_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex-2 guard: a post-snapshot SEAL inside grace is not a truncation watermark yet."""
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    original = verify._read_anchor
    appended = False

    def read_after_snapshot(path: str, **kwargs):
        nonlocal appended
        if not appended:
            appended = True
            current = _range_seals(built.audit)[-1].to_id
            at = now_utc().isoformat(timespec="microseconds")
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(f"{at} SEAL {current} {current + 1} post-snapshot\n")
        return original(path, **kwargs)

    try:
        built.audit.grace = 60.0
        with patch("firm.audit.verify._read_anchor", read_after_snapshot):
            report = built.audit.verify(full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
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
def test_replayed_valid_anchor_event_does_not_change_watermarks(
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
        assert built.audit.verify(full=True).outcome == "ok"
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


def test_anchored_floor_remains_authoritative_without_db_floor_or_old_seal_lines(
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
        assert built.audit.verify(full=True).outcome == "ok"
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


def test_activation_on_populated_keyed_table_seals_existing_rows_and_detects_deletion(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'activation-populated-keyed.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=0.0)
    try:
        row_ids = _record_old(audit, "before-activation", 3)

        assert audit.sealer.run_once() == 3
        activation = next(record for record in _records(audit) if record.kind == "activation")
        first_seal = _range_seals(audit)[0]
        assert activation.to_id == 0
        assert first_seal.from_id == 0
        assert first_seal.to_id == row_ids[-1]
        assert audit.verify(full=True).outcome == "ok"

        with transaction(audit.engine) as conn:
            conn.execute(delete(_audits).where(_audits.c.id == row_ids[0]))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_genuinely_never_sealed_keyed_log_verifies_ok(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'never-sealed-keyed.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=60.0)
    try:
        audit.record("pre-activation")
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()


def test_never_sealed_growing_keyed_log_stays_ok(tmp_path: Path) -> None:
    """Codex-3 guard: event-count growth is not evidence that sealing once existed."""
    database_url = f"sqlite:///{tmp_path / 'never-sealed-growing.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=0.0)
    try:
        for index in range(5):
            _record_old(audit, f"never-sealed-{index}", 2)
            assert audit.verify(full=True).outcome == "ok"
        with transaction(audit.engine) as conn:
            assert conn.execute(select(_status.c.sealing_observed)).scalar_one() is False
    finally:
        audit.close()


def test_anchored_wiped_events_and_side_table_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_log(tmp_path, monkeypatch, Lifecycle("sealed", True, "single"))
    try:
        with transaction(built.audit.engine) as conn:
            conn.execute(delete(_audits))
            conn.execute(delete(_seals))
        assert built.audit.verify(full=True).outcome == "tampered"
    finally:
        built.audit.close()


def test_activation_boundary_uses_grace_cutoff_and_young_row_is_later_sealed(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'activation-grace.db'}"
    audit = AuditLog(database_url=database_url, mac_key=_SINGLE_KEY, grace=3600.0)
    try:
        old = now_utc() - timedelta(hours=2)
        with transaction(audit.engine) as conn:
            conn.execute(_audits.insert().values(action="old-legacy", created_at=old))
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


def test_compaction_at_nonzero_grace_still_detects_truncation(tmp_path: Path) -> None:
    """A seal settled past ``grace`` folds into the CHECKPOINT even when compaction runs with a
    nonzero grace; deleting it afterward is still TAMPERED. Guards the compaction/grace blackout
    where a fresh checkpoint's coverage was grace-gated to zero for ``grace`` seconds."""
    database_url = f"sqlite:///{tmp_path / 'compact-grace.db'}"
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=database_url,
        mac_key=_SINGLE_KEY,
        grace=60.0,
        anchor_path=str(anchor),
        anchor_max_age=48 * 3600.0,
    )
    try:
        # A keyed row recorded two hours ago (via the clock patch), so it seals rather than
        # setting the activation boundary as a legacy NULL-mac row would.
        with patch("firm.audit.events.now_utc", lambda: now_utc() - timedelta(hours=2)):
            audit.record("covered")
        # Seal an hour ago so the seal is > grace old by the time we compact/verify at "now".
        with patch("firm.audit.sealing.now_utc", lambda: now_utc() - timedelta(hours=1)):
            assert audit.sealer.run_once() == 1
        assert audit.verify(full=True).outcome == "ok"

        coverage, _floor = audit.sealer.compact_anchor(str(anchor))
        assert coverage > 0  # the settled seal folded into the checkpoint, not grace-gated to 0
        assert anchor.read_text(encoding="utf-8").splitlines()[0].split()[1] == "CHECKPOINT"
        assert audit.verify(full=True).outcome == "ok"

        seal = _range_seals(audit)[-1]
        with transaction(audit.engine) as conn:
            conn.execute(
                delete(_audits).where(_audits.c.id > seal.from_id, _audits.c.id <= seal.to_id)
            )
            conn.execute(delete(_seals).where(_seals.c.id == seal.id))
        assert audit.verify(full=True).outcome == "tampered"
    finally:
        audit.close()


def test_malformed_anchor_line_is_skipped_not_raised(tmp_path: Path) -> None:
    """A non-ASCII byte in a MAC field and an invalid UTF-8 byte must each degrade to one skipped
    line — never a raised TypeError/UnicodeError that flips verify to a false whole-log TAMPERED
    or escapes Retention.run_once (the no-raise contract on remote/WORM anchor storage)."""
    database_url = f"sqlite:///{tmp_path / 'bad-anchor.db'}"
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=database_url,
        mac_key=_SINGLE_KEY,
        grace=0.0,
        max_age=48 * 3600.0,
        anchor_path=str(anchor),
        anchor_max_age=48 * 3600.0,
        on_error=lambda _e: None,
    )
    try:
        audit.record("covered")
        assert audit.sealer.run_once() == 1
        assert audit.verify(full=True).outcome == "ok"
        with anchor.open("ab") as handle:
            handle.write(f"{now_utc().isoformat()} FLOOR 0 café\n".encode())  # non-ASCII MAC
            handle.write(b"\xff\xfe not valid utf-8\n")  # invalid UTF-8 byte
        # Neither raises, and the still-present valid seal keeps the verdict off TAMPERED.
        assert audit.verify(full=True).outcome in {"ok", "warning"}
        audit.retention.run_once()  # must not raise on the same malformed anchor
    finally:
        audit.close()


def test_non_ascii_db_seal_mac_does_not_raise_out_of_retention(tmp_path: Path) -> None:
    """A non-ASCII seal_mac written straight into the database must make verify report tampered and
    retention refuse — never raise TypeError out of run_once (the never-raise-on-storage contract,
    which compare_digest would otherwise break on a non-ASCII tag)."""
    database_url = f"sqlite:///{tmp_path / 'nonascii-mac.db'}"
    audit = AuditLog(
        database_url=database_url,
        mac_key=_SINGLE_KEY,
        grace=0.0,
        max_age=0.0,
        on_error=lambda _e: None,
    )
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 1
        target = (_range_seals(audit)[0].to_id,)
        with transaction(audit.engine) as conn:
            conn.execute(
                update(_seals).where(_seals.c.kind == "seal").values(seal_mac="café" + "x" * 60)
            )
        assert audit.verify(full=True).outcome == "tampered"
        # Retention must REFUSE (not raise, and not launder): prune nothing, flag the refusal, and
        # leave the tampered evidence in place even though max_age=0 makes the range expired.
        assert audit.retention.run_once() == 0
        assert audit.retention.last_refused_tampered == 1
        assert _present_ids(audit, target) == set(target)
    finally:
        audit.close()


def test_transient_verify_error_preserves_sealing_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient storage error during verify must not reset the persisted ``sealing_observed``
    flag to False and silently disable the no-anchor wipe guard on the next clean run."""
    database_url = f"sqlite:///{tmp_path / 'flag.db'}"
    audit = AuditLog(
        database_url=database_url, mac_key=_SINGLE_KEY, grace=0.0, on_error=lambda _e: None
    )
    try:
        audit.record("a")
        assert audit.sealer.run_once() == 1
        assert audit.verify(full=True).outcome == "ok"  # sets sealing_observed = True

        def boom(_conn: object) -> object:
            raise SQLAlchemyError("transient blip")

        monkeypatch.setattr(verify, "load_seal_records", boom)
        assert audit.verify(full=True).outcome == "tampered"
        monkeypatch.undo()

        with transaction(audit.engine) as conn:
            row = conn.execute(
                select(schema.verify_status.c.sealing_observed).where(
                    schema.verify_status.c.id == 1
                )
            ).first()
        assert row is not None and bool(row.sealing_observed) is True
    finally:
        audit.close()
