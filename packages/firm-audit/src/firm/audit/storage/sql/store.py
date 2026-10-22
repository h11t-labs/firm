"""SQL persistence and transactional maintenance scopes for AuditLog."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import takewhile
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, delete, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ...._core.database import create_engine_for, dispose_engine, snapshot_transaction, transaction
from ...._core.dialects import get_dialect
from ...._core.dialects.base import inserted_count
from ... import events, schema
from ...object_store import ObjectConflict, Receipt
from ...prepared import PreparedEvent, decode_prepared
from ..base import ACTIVATION_FROM_ID, STATUS_ID, SealConflict

if TYPE_CHECKING:
    from ...verify import SealRecords

_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status


class SqlView:
    """One SQL connection inside a transaction, exposing the operations the algorithms need."""

    sql = True

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    # -- seal records and rows ------------------------------------------------------------------

    def seal_records(self) -> SealRecords:
        from .projections import seal_records

        return seal_records(self.conn)

    def iter_rows(self, low: int, high: int | None) -> Iterator[Any]:
        from .projections import iter_rows

        return iter_rows(self.conn, low, high)

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
        rows = self.conn.execute(
            select(_audits.c.id, _audits.c.row_mac, _audits.c.created_at)
            .where(_audits.c.id > hwm)
            .order_by(_audits.c.id)
            .limit(limit)
        ).all()
        return list(takewhile(lambda row: row.created_at <= cutoff, rows))

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
        from .archive import lock_policy

        return lock_policy(self.conn, schema_present=schema_present)

    def verified_ids(
        self, stream: str, rows: Sequence[Any], *, span: tuple[int, int] | None = None
    ) -> set[int]:
        from .archive import verified_ids

        return verified_ids(self.conn, stream, rows, span=span)

    def set_archive_policy(self, stream: str, destination: str) -> None:
        self.conn.execute(
            update(schema.archive_policy).values(stream=stream, destination=destination)
        )

    def pending_archive_batch(self) -> tuple[str, str | None] | None:
        """The oldest frozen-but-unpublished batch (locked), as ``(id, body)``."""
        batches = schema.archive_batches
        pending = self.conn.execute(
            select(batches)
            .where(batches.c.receipt.is_(None))
            .order_by(batches.c.id)
            .limit(1)
            .with_for_update()
        ).first()
        return None if pending is None else (pending.id, pending.body)

    def unreserved_rows(self, limit: int) -> list[Any]:
        """Committed rows no batch has reserved yet, oldest id first, skipping locked rows."""
        member = schema.archive_members
        dialect = get_dialect(self.conn.engine)
        # Locking reads see current reservations even under MySQL REPEATABLE READ.
        # Skip locked source rows so a delayed writer cannot stall discovery.
        unreserved = (
            ~select(member.c.event_id)
            .where(member.c.event_id == _audits.c.id)
            .with_for_update()
            .exists()
        )
        return list(
            self.conn.execute(
                dialect.with_skip_locked(
                    select(_audits).where(unreserved).order_by(_audits.c.id).limit(limit)
                )
            )
        )

    def archive_entry(self, stream: str, row: Any) -> dict[str, Any]:
        from .archive import source_entry

        return source_entry(self.conn, stream, row)

    def reserve_archive_batch(
        self, batch_id: str, body: str, members: Sequence[dict[str, Any]]
    ) -> None:
        self.conn.execute(schema.archive_batches.insert().values(id=batch_id, body=body))
        self.conn.execute(schema.archive_members.insert(), list(members))

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
    requires_entry_id = False
    stream: str | None = None

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        database_url: str | None = None,
        create_schema: bool = True,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self._owns_engine = False
        elif database_url is not None:
            self.engine = create_engine_for(database_url)
            self._owns_engine = True
        else:
            raise ValueError("AuditLog requires either database_url or engine (or storage=)")
        try:
            if create_schema:
                schema.create_all(self.engine)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._owns_engine:
            dispose_engine(self.engine)

    def record(
        self,
        values: dict[str, Any],
        *,
        conn: Connection | None = None,
        grace: float,
    ) -> None:
        """Insert the exact values already signed by the audit log."""
        if conn is not None:
            conn.execute(_audits.insert().values(**values))
        else:
            with transaction(self.engine) as own:
                own.execute(_audits.insert().values(**values))

    def write(self, prepared: PreparedEvent, *, grace: float) -> int:
        """Idempotently insert a prepared event under its unique entry id, checking its bytes."""
        import json

        from .archive import import_provenance

        values = decode_prepared(prepared)
        provenance = values.pop("provenance", None)
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        dialect = get_dialect(self.engine)
        with dialect.begin_claim_tx(self.engine) as conn:
            result = conn.execute(
                dialect.insert_ignore(_audits, values, index_elements=["entry_id"])
            )
            inserted = inserted_count(result)
            row = conn.execute(
                select(_audits).where(_audits.c.entry_id == values["entry_id"])
            ).one()
            if any(row._mapping[name] != value for name, value in values.items()):
                raise ObjectConflict("prepared event identity already contains different bytes")
            if inserted:
                if provenance is not None:
                    conn.execute(
                        schema.archive_imports.insert().values(
                            event_id=row.id,
                            provenance=json.dumps(provenance),
                        )
                    )
            elif import_provenance(conn, row.id) != provenance:
                raise ObjectConflict("prepared event identity has different provenance")
            return row.id

    def get(self, locator: int | Receipt | str) -> dict[str, Any] | None:
        if not isinstance(locator, int):
            raise ValueError("SQL audit events are located by integer id")
        with transaction(self.engine) as conn:
            return events.get(conn, locator)

    def history(
        self,
        wanted: dict[str, str],
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        stmt = select(_audits).order_by(_audits.c.id.desc()).limit(limit)
        for name, value in wanted.items():
            stmt = stmt.where(_audits.c[name] == value)
        if since is not None:
            if since.tzinfo is not None:
                since = since.astimezone(UTC).replace(tzinfo=None)
            stmt = stmt.where(_audits.c.created_at >= since)
        with transaction(self.engine) as conn:
            return [events._row_to_dict(row) for row in conn.execute(stmt)]

    def import_provenance(self, event_id: int) -> dict[str, Any] | None:
        from .archive import import_provenance

        with transaction(self.engine) as conn:
            return import_provenance(conn, event_id)

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

    def acknowledge_archive_batch(self, batch_id: str, receipt: str) -> None:
        """Record a verified publication and drop the frozen input, in its own transaction."""
        batches = schema.archive_batches
        with transaction(self.engine) as conn:
            conn.execute(
                update(batches).where(batches.c.id == batch_id).values(receipt=receipt, body=None)
            )

    def archive_status(self) -> dict[str, int]:
        with transaction(self.engine) as conn:
            total = conn.execute(select(func.count()).select_from(_audits)).scalar_one()
            pending = conn.execute(
                select(func.count())
                .select_from(schema.archive_batches)
                .where(schema.archive_batches.c.receipt.is_(None))
            ).scalar_one()
            member, batches = schema.archive_members, schema.archive_batches
            archived = conn.execute(
                select(func.count())
                .select_from(
                    _audits.join(member, _audits.c.id == member.c.event_id).join(
                        batches, member.c.batch_id == batches.c.id
                    )
                )
                .where(batches.c.receipt.is_not(None))
            ).scalar_one()
        return {"events": total, "unarchived": total - archived, "pending_batches": pending}

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
