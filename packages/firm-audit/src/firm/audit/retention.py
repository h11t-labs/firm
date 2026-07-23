"""Opt-in retention with one signed, append-only retirement floor.

Without activation, retention keeps its original age-based delete path. Once sealing is active,
it only prunes complete independent seal ranges. Every candidate range is reclassified with the
same exact classifier used by verify and its seal MAC is rechecked. The first young range stops
the boundary; any tampered range refuses the operation and preserves all evidence.

For an aligned prune, verification, the new ``floor`` row, event deletion, and covering-seal
deletion share one write transaction. If an anchor sink is configured, the FLOOR line is appended
first and is a hard gate: a sink failure rolls back/refuses the prune. Floor advances themselves
are never updated or deleted.

Because the anchor line lands before the database commit, a crash or serialization rollback in
between leaves the anchor floor leading the database — with the rows and their covering seals
still fully present. Both retention and verify recognize that state as an interrupted prune
(rather than tampering): retention resumes it from the database floor after re-validating the
ranges, and verify reports a warning until it does.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError

from .._core.clock import now_utc
from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .integrity import HmacSigner
from .sealing import _ACTIVATION_FROM_ID
from .verify import (
    _read_anchor,
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
_TRANSACTION_ATTEMPTS = 3


class Retention:
    """Delete expired history only when doing so preserves the tamper-evidence invariant."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.last_skipped_unsealed = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False
        self.last_refused_no_activation = False

    def run_once(self) -> int:
        self.last_skipped_unsealed = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False
        self.last_refused_no_activation = False
        if self.audit.max_age is None:
            return 0
        cutoff = now_utc() - timedelta(seconds=self.audit.max_age)
        if self._sealing_active():
            return self._run_aligned(cutoff)
        if self.audit._seal_key is not None and self._has_expired_events(cutoff):
            self._refuse_no_activation()
            return 0
        return self._run_plain(cutoff)

    def _sealing_active(self) -> bool:
        """Any activation/seal/floor record makes plain pruning unsafe."""
        with self.audit.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(_seals)).scalar_one() > 0

    def _has_expired_events(self, cutoff: datetime) -> bool:
        with self.audit.engine.connect() as conn:
            return (
                conn.execute(
                    select(_audits.c.id).where(_audits.c.created_at < cutoff).limit(1)
                ).first()
                is not None
            )

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
        for attempt in range(_TRANSACTION_ATTEMPTS):
            try:
                return self._run_aligned_once(cutoff)
            except DBAPIError as exc:
                if not _is_retryable_transaction_error(exc):
                    raise
                if attempt + 1 == _TRANSACTION_ATTEMPTS:
                    self.audit.on_error(exc)
                    return 0
                time.sleep(0.01 * (attempt + 1))
        raise AssertionError("unreachable")

    def _run_aligned_once(self, cutoff: datetime) -> int:
        seal_key = self.audit._seal_key
        if seal_key is None:
            self._refuse_no_seal_key(None)
            return 0

        deleted = 0
        with snapshot_transaction(self.audit.engine, write=True) as conn:
            dialect = get_dialect(self.audit.engine)
            # SQLite is serialized by BEGIN IMMEDIATE. PostgreSQL/MySQL lock the never-deleted
            # activation row before either side computes a shared high-water mark / floor.
            activation_lock = dialect.with_row_lock(
                # Lock by the reserved, uniquely-indexed ``from_id`` (not the un-indexed ``kind``):
                # on MySQL a FOR UPDATE over a non-indexed predicate escalates to a table/gap lock.
                select(_seals.c.id).where(_seals.c.from_id == _ACTIVATION_FROM_ID).limit(1)
            )
            conn.execute(activation_lock).first()
            # The first database read fixes the snapshot before the external anchor is consumed.
            records = load_seal_records(conn)
            if records.capped:
                self._refuse_tampered(None)
                return 0
            keyring = self.audit.verifier.keyring
            seal_keyring = self.audit.verifier.seal_keyring

            unknown = [
                record
                for record in records
                if record.key_id is not None and record.key_id not in seal_keyring
            ]
            if unknown:
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

            database_floor = max((record.to_id or 0 for record in floors), default=0)
            floor = database_floor
            if self.audit._anchor_path is not None:
                anchor = _read_anchor(
                    self.audit._anchor_path,
                    coverage_cutoff=now_utc() - timedelta(seconds=self.audit.grace),
                    seal_keyring=seal_keyring,
                )
                floor = max(floor, anchor.floor_watermark)
                present_coverage = max(
                    [
                        floor,
                        *(
                            record.to_id or 0
                            for record in records
                            if record.kind == "seal" and seal_is_intact(record, seal_keyring)
                        ),
                    ]
                )
                if present_coverage < anchor.coverage_watermark:
                    self._refuse_tampered(floors[-1] if floors else None)
                    return 0
            if (
                database_floor > 0
                and conn.execute(
                    select(_audits.c.id).where(_audits.c.id <= database_floor).limit(1)
                ).first()
            ):
                self._refuse_tampered(floors[-1] if floors else None)
                return 0
            if (
                floor > database_floor
                and conn.execute(
                    select(_audits.c.id)
                    .where(_audits.c.id > database_floor, _audits.c.id <= floor)
                    .limit(1)
                ).first()
            ):
                # An earlier prune appended its anchor FLOOR line but its database transaction
                # never committed (crash, or the serialization rollback this method retries on):
                # the rows *and their covering seals* in (database_floor, floor] are all still
                # present. Resume from the database floor — the ranges are re-validated and
                # pruned again below — instead of refusing forever on a floor the database
                # never reached. Rows in the gap without contiguous seal coverage are not that
                # state (a forged restore, or seal loss) and refuse loudly as before.
                if self._gap_is_seal_covered(records, database_floor, boundary, floor):
                    floor = database_floor
                else:
                    self._refuse_tampered(floors[-1] if floors else None)
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
            signer = HmacSigner(seal_key)
            mac = signer.sign(
                integrity.floor_mac_input(
                    through_id=through,
                    retired_at=retired_at,
                    key_id=signer.key_id,
                )
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

    @staticmethod
    def _gap_is_seal_covered(
        records: Sequence[SealRecord], database_floor: int, boundary: int, floor: int
    ) -> bool:
        """True when contiguous seal records span (database_floor, floor] — the signature of an
        interrupted prune's rollback, which restores the deleted seals along with the rows.
        Every record was already MAC-verified above, so kind is all that needs checking."""
        position = max(database_floor, boundary)
        seals = sorted(
            (record for record in records if record.kind == "seal"),
            key=lambda record: (record.from_id or -1, record.id),
        )
        for record in seals:
            if position >= floor:
                break
            if record.from_id == position and record.to_id is not None:
                position = record.to_id
        return position >= floor

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

    def _refuse_no_activation(self) -> None:
        self.last_refused_no_activation = True
        self.audit.on_error(
            RuntimeError(
                "audit retention REFUSED to plain-prune expired signed rows: a seal key is "
                "configured but no activation marker exists"
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


def _is_retryable_transaction_error(exc: DBAPIError) -> bool:
    """Recognize serialization/deadlock failures across supported DBAPI drivers."""
    original = exc.orig
    state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if state in {"40001", "40P01"}:
        return True
    args = getattr(original, "args", ())
    if args and args[0] in {1205, 1213}:
        return True
    message = str(original).lower()
    return any(
        phrase in message for phrase in ("serialization failure", "deadlock", "database is locked")
    )
