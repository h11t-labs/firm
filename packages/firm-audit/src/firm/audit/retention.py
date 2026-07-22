"""Opt-in retention with one signed, append-only retirement floor.

Without activation, retention keeps its original age-based delete path. Once sealing is active,
it only prunes complete independent seal ranges. Every candidate range is reclassified with the
same exact classifier used by verify and its seal MAC is rechecked. The first young range stops
the boundary; any tampered range refuses the operation and preserves all evidence.

For an aligned prune, verification, the new ``floor`` row, event deletion, and covering-seal
deletion share one write transaction. If an anchor sink is configured, the FLOOR line is appended
first and is a hard gate: a sink failure rolls back/refuses the prune. Floor advances themselves
are never updated or deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from .._core.clock import now_utc
from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .verify import (
    _classify_anchor_issues,
    _read_anchor,
    _record_matches_anchor,
    load_seal_records,
    range_is_prunable,
    seal_is_intact,
)

if TYPE_CHECKING:
    from .log import AuditLog
    from .verify import SealRecord

_audits = schema.audit_events
_seals = schema.seals
_BATCH_SIZE = 1000
_SKIP_ALERT_THRESHOLD = 1000


class Retention:
    """Delete expired history only when doing so preserves the tamper-evidence invariant."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.last_skipped_unsealed = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False

    def run_once(self) -> int:
        self.last_skipped_unsealed = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False
        if self.audit.max_age is None:
            return 0
        cutoff = now_utc() - timedelta(seconds=self.audit.max_age)
        if self._sealing_active():
            return self._run_aligned(cutoff)
        return self._run_plain(cutoff)

    def _sealing_active(self) -> bool:
        """Any activation/seal/floor record makes plain pruning unsafe."""
        with self.audit.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(_seals)).scalar_one() > 0

    def _run_plain(self, cutoff: datetime) -> int:
        engine = self.audit.engine
        dialect = get_dialect(engine)
        total = 0
        while True:
            with dialect.begin_claim_tx(engine) as conn:
                stmt = dialect.with_skip_locked(
                    select(_audits.c.id).where(_audits.c.created_at < cutoff).limit(_BATCH_SIZE)
                )
                ids = [row.id for row in conn.execute(stmt)]
                if ids:
                    conn.execute(delete(_audits).where(_audits.c.id.in_(ids)))
            total += len(ids)
            if len(ids) < _BATCH_SIZE:
                return total

    def _run_aligned(self, cutoff: datetime) -> int:
        seal_key = self.audit._seal_key
        if seal_key is None:
            self._refuse_no_seal_key(None)
            return 0

        deleted = 0
        with snapshot_transaction(self.audit.engine, write=True) as conn:
            records = load_seal_records(conn)
            keyring = self.audit.verifier.keyring
            seal_keyring = self.audit.verifier.seal_keyring

            unknown = [
                record
                for record in records
                if record.key_id is not None and record.key_id not in seal_keyring
            ]
            row_key_only = (
                self.audit._key is not None
                and self.audit._seal_key is not None
                and self.audit._key.secret == self.audit._seal_key.secret
            )
            if unknown and row_key_only:
                self._refuse_no_seal_key(unknown[0])
                return 0
            for record in records:
                if not seal_is_intact(record, seal_keyring):
                    self._refuse_tampered(record)
                    return 0

            activations = [record for record in records if record.kind == "activation"]
            if len(activations) != 1:
                self._refuse_tampered(activations[0] if activations else None)
                return 0
            boundary = activations[0].to_id or 0

            floors = [record for record in records if record.kind == "floor"]
            previous = -1
            for floor_record in floors:
                through = floor_record.to_id or 0
                if through <= previous:
                    self._refuse_tampered(floor_record)
                    return 0
                previous = through

            floor = max((record.to_id or 0 for record in floors), default=0)
            if self.audit._anchor_path is not None:
                anchor = _read_anchor(self.audit._anchor_path)
                _, corrupt = _classify_anchor_issues(anchor, records, retired_through=floor)
                if corrupt:
                    self._refuse_tampered(floors[-1] if floors else None)
                    return 0
                for floor_record in floors:
                    if not any(
                        _record_matches_anchor(floor_record, event) for event in anchor.events
                    ):
                        self._refuse_tampered(floor_record)
                        return 0

            covering = [
                record
                for record in records
                if record.kind == "seal" and (record.to_id or 0) > floor
            ]
            covering.sort(key=lambda record: (record.from_id or -1, record.id))
            origin = max(floor, boundary)
            if covering and covering[0].from_id != origin:
                self._refuse_tampered(covering[0])
                return 0
            for previous_seal, current in pairwise(covering):
                if current.from_id != previous_seal.to_id:
                    self._refuse_tampered(current)
                    return 0

            max_sealed = max([origin, *(record.to_id or origin for record in covering)])
            self.last_skipped_unsealed = conn.execute(
                select(func.count())
                .select_from(_audits)
                .where(_audits.c.id > max_sealed, _audits.c.created_at < cutoff)
            ).scalar_one()
            self._alert_if_over_threshold()

            through = floor
            for record in covering:
                assert record.sealed_at is not None
                if record.sealed_at >= cutoff:
                    break
                if not range_is_prunable(conn, record, boundary, keyring, seal_keyring):
                    self._refuse_tampered(record)
                    return 0
                assert record.to_id is not None
                through = record.to_id
            if through <= floor:
                return 0

            retired_at = now_utc()
            mac = integrity.floor_mac(
                seal_key,
                through_id=through,
                retired_at=retired_at,
                key_id=seal_key.id,
            )
            if not self.audit.sealer._emit_anchor(
                kind="floor", from_id=None, to_id=through, mac=mac, at=retired_at
            ):
                return 0

            conn.execute(
                _seals.insert().values(
                    kind="floor",
                    from_id=None,
                    to_id=through,
                    row_count=None,
                    rows_mac=None,
                    seal_mac=mac,
                    sealed_at=retired_at,
                    key_id=seal_key.id,
                )
            )
            deleted = conn.execute(delete(_audits).where(_audits.c.id <= through)).rowcount
            conn.execute(delete(_seals).where(_seals.c.kind == "seal", _seals.c.to_id <= through))

        return deleted

    def _refuse_tampered(self, record: SealRecord | None) -> None:
        self.last_refused_tampered += 1
        label = f"{record.kind} record {record.id}" if record is not None else "activation state"
        self.audit.on_error(
            RuntimeError(
                f"audit retention REFUSED to prune: {label} is malformed, unauthentic, "
                "non-contiguous, or its rows no longer verify"
            )
        )

    def _refuse_no_seal_key(self, record: SealRecord | None) -> None:
        self.last_refused_no_seal_key = True
        signer = record.key_id if record is not None else None
        self.audit.on_error(
            RuntimeError(
                "audit retention REFUSED to prune: this host does not have the seal key needed "
                f"to validate/sign floors (existing signer key_id={signer!r})"
            )
        )

    def _alert_if_over_threshold(self) -> None:
        if self.last_skipped_unsealed > _SKIP_ALERT_THRESHOLD:
            self.audit.on_error(
                RuntimeError(
                    f"audit retention skipped {self.last_skipped_unsealed} expired but UNSEALED "
                    "rows; the sealer looks stalled"
                )
            )


class RetentionLoop(InterruptiblePoller):
    """Optional background retention poller."""

    def __init__(
        self,
        retention: Retention,
        interval: float = 3600.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="audit-retention", on_error=on_error)
        self.retention = retention

    def poll(self) -> int:
        return self.retention.run_once()
