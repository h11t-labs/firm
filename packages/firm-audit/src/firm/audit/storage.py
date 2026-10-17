"""The storage seam under the tamper-evidence algorithms.

Sealing, verification and retention decide *what* to read and write; a storage decides *how*.
:class:`SqlStorage` is the audit tables as they have always been (every statement here moved
verbatim out of :mod:`.sealing`, :mod:`.verify` and :mod:`.retention`); the S3 backend implements
the same operations on objects. The algorithms only ever talk to a *view*: one consistent read or
one exclusive write scope, handed out by the storage's context managers.

A view's methods are operations, not statements: "the highest seal coverage", "settled rows after
this id", "advance the floor to this id". Each storage keeps its own concurrency story behind them
(SQL: snapshot/claim transactions and row locks; S3: an exclusive maintenance lease).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, delete, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from . import schema

if TYPE_CHECKING:
    from .verify import SealRecords

_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status

#: The reserved ``from_id`` of the activation record (a real event id is positive).
ACTIVATION_FROM_ID = -1
#: The single verify-status row's primary key.
STATUS_ID = 1


class SealConflict(Exception):
    """Another sealer already wrote a record at this coordinate (the portable race arbiter)."""


class SqlView:
    """One SQL connection inside a transaction, exposing the operations the algorithms need."""

    sql = True

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    # -- seal records and rows ------------------------------------------------------------------

    def seal_records(self) -> SealRecords:
        from .verify import _sql_seal_records

        return _sql_seal_records(self.conn)

    def iter_rows(self, low: int, high: int | None) -> Iterator[Any]:
        from .verify import _sql_iter_rows

        return _sql_iter_rows(self.conn, low, high)

    def any_seal_record(self) -> bool:
        return self.conn.execute(select(func.count()).select_from(_seals)).scalar_one() > 0

    def activation_exists(self) -> bool:
        return (
            self.conn.execute(
                select(_seals.c.id).where(_seals.c.kind == "activation").limit(1)
            ).first()
            is not None
        )

    def lock_activation(self) -> bool:
        """Lock the never-deleted activation row (PostgreSQL/MySQL); ``False`` when absent."""
        dialect = get_dialect(self.conn.engine)
        # Lock by the reserved, uniquely-indexed ``from_id`` (not the un-indexed ``kind``):
        # on MySQL a FOR UPDATE over a non-indexed predicate escalates to a table/gap lock.
        stmt = dialect.with_row_lock(
            select(_seals.c.id).where(_seals.c.from_id == ACTIVATION_FROM_ID).limit(1)
        )
        return self.conn.execute(stmt).first() is not None

    def signer_mismatch(self, allowed: Sequence[str]) -> tuple[bool, str | None]:
        """Whether any seal record has a NULL or non-``allowed`` signer, and the first such id."""
        row = self.conn.execute(
            select(_seals.c.key_id)
            .where(or_(_seals.c.key_id.is_(None), _seals.c.key_id.not_in(tuple(allowed))))
            .limit(1)
        ).first()
        return (row is not None, row.key_id if row is not None else None)

    def seal_hwm(self) -> int:
        return (
            self.conn.execute(
                select(func.max(_seals.c.to_id)).where(
                    _seals.c.kind.in_(("activation", "floor", "seal"))
                )
            ).scalar_one()
            or 0
        )

    def max_unsigned_settled_id(self, cutoff: datetime) -> int:
        return (
            self.conn.execute(
                select(func.max(_audits.c.id)).where(
                    _audits.c.row_mac.is_(None), _audits.c.created_at <= cutoff
                )
            ).scalar_one()
            or 0
        )

    def settled_rows_after(self, hwm: int, cutoff: datetime, limit: int) -> list[Any]:
        return list(
            self.conn.execute(
                select(_audits.c.id, _audits.c.row_mac)
                .where(_audits.c.id > hwm, _audits.c.created_at <= cutoff)
                .order_by(_audits.c.id)
                .limit(limit)
            ).all()
        )

    def insert_seal(self, **values: Any) -> None:
        try:
            self.conn.execute(_seals.insert().values(**values))
        except IntegrityError as exc:
            raise SealConflict(str(exc)) from exc

    # -- row presence -----------------------------------------------------------------------------

    def any_row(self) -> bool:
        return self.conn.execute(select(_audits.c.id).limit(1)).first() is not None

    def any_row_between(self, low: int, high: int) -> bool:
        """Whether any event id lies in ``(low, high]``."""
        return (
            self.conn.execute(
                select(_audits.c.id).where(_audits.c.id > low, _audits.c.id <= high).limit(1)
            ).first()
            is not None
        )

    def rows_at_or_below(self, floor: int, limit: int) -> list[int]:
        rows = self.conn.execute(
            select(_audits.c.id).where(_audits.c.id <= floor).order_by(_audits.c.id).limit(limit)
        ).all()
        return [row.id for row in rows]

    def duplicate_entry_ids(self, limit: int) -> list[str]:
        rows = self.conn.execute(
            select(_audits.c.entry_id)
            .where(_audits.c.entry_id.is_not(None))
            .group_by(_audits.c.entry_id)
            .having(func.count() > 1)
            .limit(limit)
        ).all()
        return [row.entry_id for row in rows]

    def has_expired(self, cutoff: datetime) -> bool:
        return (
            self.conn.execute(
                select(_audits.c.id).where(_audits.c.created_at < cutoff).limit(1)
            ).first()
            is not None
        )

    def count_rows(self, *, after: int, before: datetime, through: int | None = None) -> int:
        """Count events with id > ``after`` (and <= ``through``) created before ``before``."""
        stmt = (
            select(func.count())
            .select_from(_audits)
            .where(_audits.c.id > after, _audits.c.created_at < before)
        )
        if through is not None:
            stmt = stmt.where(_audits.c.id <= through)
        return self.conn.execute(stmt).scalar_one()

    def rows_between(self, low: int, high: int) -> list[Any]:
        """Full rows with id in ``(low, high]`` (the archive guard's candidates)."""
        return list(
            self.conn.execute(select(_audits).where(_audits.c.id > low, _audits.c.id <= high)).all()
        )

    # -- archive-before-prune policy --------------------------------------------------------------

    def archive_policy(self, *, schema_present: bool | None = None) -> Any:
        from .sql_archive import lock_policy

        return lock_policy(self.conn, schema_present=schema_present)

    def verified_ids(
        self, stream: str, rows: Sequence[Any], *, span: tuple[int, int] | None = None
    ) -> set[int]:
        from .sql_archive import verified_ids

        return verified_ids(self.conn, stream, rows, span=span)

    # -- pruning ----------------------------------------------------------------------------------

    def expired_batch(self, cutoff: datetime, offset: int, limit: int) -> list[Any]:
        """One batch of expired rows, skipping rows a concurrent pruner holds."""
        dialect = get_dialect(self.conn.engine)
        stmt = dialect.with_skip_locked(
            select(_audits)
            .where(_audits.c.created_at < cutoff)
            .order_by(_audits.c.id)
            .offset(offset)
            .limit(limit)
        )
        return list(self.conn.execute(stmt).all())

    def delete_rows(self, ids: Sequence[int], *, archive_tables: bool) -> None:
        self.conn.execute(delete(_audits).where(_audits.c.id.in_(ids)))
        if archive_tables:
            self.conn.execute(
                delete(schema.archive_imports).where(schema.archive_imports.c.event_id.in_(ids))
            )
            self.conn.execute(
                delete(schema.archive_members).where(schema.archive_members.c.event_id.in_(ids))
            )

    def advance_floor(self, floor: dict[str, Any], *, through: int, archive_tables: bool) -> int:
        """Insert the floor record, delete events and covered seals through ``through``, in this
        one transaction. Returns the number of events deleted."""
        self.conn.execute(_seals.insert().values(**floor))
        deleted = self.conn.execute(delete(_audits).where(_audits.c.id <= through)).rowcount
        if archive_tables:
            self.conn.execute(
                delete(schema.archive_imports).where(schema.archive_imports.c.event_id <= through)
            )
            self.conn.execute(
                delete(schema.archive_members).where(schema.archive_members.c.event_id <= through)
            )
        self.conn.execute(delete(_seals).where(_seals.c.kind == "seal", _seals.c.to_id <= through))
        return deleted

    # -- verify status ----------------------------------------------------------------------------

    def status_prior(self) -> Any:
        return self.conn.execute(
            select(_status.c.last_full_coverage_at, _status.c.sealing_observed).where(
                _status.c.id == STATUS_ID
            )
        ).first()


class SqlStorage:
    """The SQL audit tables behind the storage seam (the default for :class:`~.log.AuditLog`)."""

    sql = True

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def view(self, conn: Connection) -> SqlView:
        return SqlView(conn)

    @contextmanager
    def snapshot(self, *, write: bool = False) -> Iterator[SqlView]:
        """One consistent snapshot (``write=True`` for the aligned prune's serializable write)."""
        with snapshot_transaction(self.engine, write=write) as conn:
            yield SqlView(conn)

    @contextmanager
    def claim(self) -> Iterator[SqlView]:
        """A short write transaction that serializes racing writers (``BEGIN IMMEDIATE`` etc.)."""
        with get_dialect(self.engine).begin_claim_tx(self.engine) as conn:
            yield SqlView(conn)

    @contextmanager
    def connect(self) -> Iterator[SqlView]:
        """A plain connection for an unlocked read."""
        with self.engine.connect() as conn:
            yield SqlView(conn)

    def prior_sealing_observed(self) -> bool:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(_status.c.sealing_observed).where(_status.c.id == STATUS_ID)
            ).scalar_one_or_none()
        return bool(value)

    def upsert_status(self, values: dict[str, Any]) -> None:
        dialect = get_dialect(self.engine)
        payload = {"id": STATUS_ID, **values}
        stmt = dialect.upsert(
            _status,
            payload,
            index_elements=["id"],
            update_columns=[column for column in payload if column != "id"],
        )
        with dialect.begin_claim_tx(self.engine) as conn:
            conn.execute(stmt)
