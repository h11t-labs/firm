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

With a persisted archive policy (``firm.audit.sql_archive.SQLArchiver.enable``), both paths keep
every expired row whose exact content is not yet in a verified archive batch; the aligned path
stops at the first range holding such a row. Eligibility is one query per batch or range.
``last_blocked_unarchived`` reports how many expired rows a run kept for that reason, and past
``_SKIP_ALERT_THRESHOLD`` the run routes an "archiver looks stalled" alert through ``on_error``,
like the unsealed-row alert — so a stalled archiver never silently halts pruning.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from sqlalchemy.exc import DBAPIError

from .._core.clock import now_utc
from .._core.poller import InterruptiblePoller
from . import integrity
from .integrity import HmacSigner
from .verify import (
    _read_anchor,
    load_seal_records,
    range_is_prunable,
    seal_is_intact,
)

if TYPE_CHECKING:
    from .log import TamperEvidentLog
    from .verify import SealRecord

_BATCH_SIZE = 1000
_SKIP_ALERT_THRESHOLD = 1000
_TRANSACTION_ATTEMPTS = 3


class Retention:
    """Delete expired history only when doing so preserves the tamper-evidence invariant."""

    def __init__(self, audit: TamperEvidentLog) -> None:
        self.audit = audit
        self.last_skipped_unsealed = 0
        self.last_blocked_unarchived = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False
        self.last_refused_no_activation = False

    def run_once(self) -> int:
        self.last_skipped_unsealed = 0
        self.last_blocked_unarchived = 0
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
        with self.audit.storage.connect() as conn:
            return conn.any_seal_record()

    def _has_expired_events(self, cutoff: datetime) -> bool:
        with self.audit.storage.connect() as conn:
            return conn.has_expired(cutoff)

    def _run_plain(self, cutoff: datetime) -> int:
        total = 0
        offset = 0
        schema_present: bool | None = None  # inspected once, on the first batch
        while True:
            with self.audit.storage.claim() as conn:
                policy = conn.archive_policy(schema_present=schema_present)
                schema_present = policy is not None
                rows = conn.expired_batch(cutoff, offset, _BATCH_SIZE)
                if policy is not None and policy.stream:
                    # One unlocked eligibility read per batch; the archive tables never join the
                    # SKIP LOCKED select (PostgreSQL rejects FOR UPDATE on an outer join's
                    # nullable side).
                    archived = conn.verified_ids(policy.stream, rows)
                    ids = [row.id for row in rows if row.id in archived]
                else:
                    ids = [row.id for row in rows]
                offset += len(rows) - len(ids)
                self.last_blocked_unarchived += len(rows) - len(ids)
                if ids:
                    conn.delete_rows(ids, archive_tables=policy is not None)
            total += len(ids)
            if len(rows) < _BATCH_SIZE:
                self._alert_if_blocked_over_threshold()
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
        with self.audit.storage.snapshot(write=True) as conn:
            policy = conn.archive_policy()
            # SQLite is serialized by BEGIN IMMEDIATE. PostgreSQL/MySQL lock the never-deleted
            # activation row before either side computes a shared high-water mark / floor.
            conn.lock_activation()
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
            if database_floor > 0 and conn.rows_at_or_below(database_floor, 1):
                self._refuse_tampered(floors[-1] if floors else None)
                return 0
            if floor > database_floor and conn.any_row_between(database_floor, floor):
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
            self.last_skipped_unsealed = conn.count_rows(after=max_sealed, before=cutoff)
            self._alert_if_over_threshold()

            through = floor
            archive_checked_through = 0
            blocked_by_archive = False
            for record in covering:
                assert record.sealed_at is not None
                if record.sealed_at >= cutoff:
                    break
                if not range_is_prunable(conn, record, boundary, keyring, seal_keyring):
                    self._refuse_tampered(record)
                    return 0
                assert record.to_id is not None
                if policy is not None and policy.stream:
                    # Include the pre-activation prefix too: the eventual delete covers every
                    # row through the new floor, not just rows belonging to the newest seal.
                    span = (archive_checked_through, record.to_id)
                    candidates = conn.rows_between(span[0], span[1])
                    if len(conn.verified_ids(policy.stream, candidates, span=span)) != len(
                        candidates
                    ):
                        blocked_by_archive = True
                        break
                    archive_checked_through = record.to_id
                through = record.to_id
            if blocked_by_archive:
                # Everything expired between the stop point and the sealed high-water mark is
                # held back by the guard — the counterpart of ``last_skipped_unsealed`` above.
                self.last_blocked_unarchived = conn.count_rows(
                    after=through, through=max_sealed, before=cutoff
                )
                self._alert_if_blocked_over_threshold()
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

            deleted = conn.advance_floor(
                {
                    "kind": "floor",
                    "from_id": None,
                    "to_id": through,
                    "row_count": None,
                    "rows_mac": None,
                    "seal_mac": mac,
                    "sealed_at": retired_at,
                    "key_id": seal_key.id,
                },
                through=through,
                archive_tables=policy is not None,
            )

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

    def _alert_if_blocked_over_threshold(self) -> None:
        if self.last_blocked_unarchived > _SKIP_ALERT_THRESHOLD:
            self.audit.on_error(
                RuntimeError(
                    f"audit retention kept {self.last_blocked_unarchived} expired rows that are "
                    "not yet in a verified archive batch; the archiver looks stalled"
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
