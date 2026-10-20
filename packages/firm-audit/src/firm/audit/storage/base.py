"""Typed persistence contract shared by audit storage backends.

Audit policy decides what to read and write; each backend owns consistency, concurrency and
persistence through Storage and StorageView.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import Connection

from ..object_store import Receipt
from ..prepared import PreparedEvent

if TYPE_CHECKING:
    from ..verify import SealRecords

#: The reserved ``from_id`` of the activation record (a real event id is positive).
ACTIVATION_FROM_ID = -1
#: The single verify-status row's primary key.
STATUS_ID = 1


class SealConflict(Exception):
    """Another sealer already wrote a record at this coordinate (the portable race arbiter)."""


class StorageBusy(RuntimeError):
    """Another process holds the storage's maintenance lease (S3); SQL never raises it."""


class StorageView(Protocol):
    """Row, seal and archive operations inside one adapter-controlled consistency scope."""

    def seal_records(self) -> SealRecords: ...

    def iter_rows(self, low: int, high: int | None) -> Iterator[Any]: ...

    def any_seal_record(self) -> bool: ...

    def activation_exists(self) -> bool: ...

    def lock_activation(self) -> bool: ...

    def signer_mismatch(self, allowed: Sequence[str]) -> tuple[bool, str | None]: ...

    def seal_hwm(self) -> int: ...

    def max_unsigned_settled_id(self, cutoff: datetime) -> int: ...

    def settled_rows_after(self, hwm: int, cutoff: datetime, limit: int) -> list[Any]: ...

    def insert_seal(self, **values: Any) -> None: ...

    def any_row(self) -> bool: ...

    def any_row_between(self, low: int, high: int) -> bool: ...

    def rows_at_or_below(self, floor: int, limit: int) -> list[int]: ...

    def duplicate_entry_ids(self, limit: int) -> list[str]: ...

    def has_expired(self, cutoff: datetime) -> bool: ...

    def count_rows(self, *, after: int, before: datetime, through: int | None = None) -> int: ...

    def rows_between(self, low: int, high: int) -> list[Any]: ...

    def archive_policy(self, *, schema_present: bool | None = None) -> Any: ...

    def verified_ids(
        self, stream: str, rows: Sequence[Any], *, span: tuple[int, int] | None = None
    ) -> set[int]: ...

    def set_archive_policy(self, stream: str, destination: str) -> None: ...

    def pending_archive_batch(self) -> tuple[str, str | None] | None: ...

    def unreserved_rows(self, limit: int) -> list[Any]: ...

    def archive_entry(self, stream: str, row: Any) -> dict[str, Any]: ...

    def reserve_archive_batch(
        self, batch_id: str, body: str, members: Sequence[dict[str, Any]]
    ) -> None: ...

    def expired_batch(self, cutoff: datetime, offset: int, limit: int) -> list[Any]: ...

    def delete_rows(self, ids: Sequence[int], *, archive_tables: bool) -> None: ...

    def advance_floor(
        self, floor: dict[str, Any], *, through: int, archive_tables: bool
    ) -> int: ...

    def status_prior(self) -> Any: ...


class Storage(Protocol):
    """Persistence required by AuditLog; keys and audit policy stay above this seam.

    record stores fully formed values, optionally in a caller transaction; write retries a
    frozen event. history/get return decoded public events. Maintenance scopes expose the
    existing row/seal/archive view operations, with consistency enforced by the adapter.
    AuditLog closes its adapter; adapters must preserve caller-owned engines and SDK clients.
    """

    @property
    def requires_entry_id(self) -> bool: ...

    @property
    def stream(self) -> str | None: ...

    def record(
        self,
        values: dict[str, Any],
        *,
        conn: Connection | None,
        grace: float,
    ) -> Receipt | None: ...

    def write(self, prepared: PreparedEvent, *, grace: float) -> int | Receipt: ...

    def get(self, locator: int | Receipt | str) -> dict[str, Any] | None: ...

    def history(
        self,
        wanted: dict[str, str],
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def import_provenance(self, event_id: int) -> dict[str, Any] | None: ...

    def snapshot(self, *, write: bool = False) -> AbstractContextManager[StorageView]: ...

    def claim(self) -> AbstractContextManager[StorageView]: ...

    def connect(self) -> AbstractContextManager[StorageView]: ...

    def prior_sealing_observed(self) -> bool: ...

    def upsert_status(self, values: dict[str, Any]) -> None: ...

    def acknowledge_archive_batch(self, batch_id: str, receipt: str) -> None: ...

    def archive_status(self) -> dict[str, int]: ...

    def close(self) -> None: ...
