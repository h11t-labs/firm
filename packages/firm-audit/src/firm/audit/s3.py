"""The S3 audit log: the :class:`~firm.audit.log.AuditLog`'s functionality with S3 as storage.

``S3AuditLog`` takes the same arguments and offers the same ``record()``, ``history()``,
``verify()``, retention, sealing, anchors and background loops as ``AuditLog``; both inherit them
from :class:`~firm.audit.log.TamperEvidentLog`, and the Sealer, Verifier and Retention code is
literally shared, running over :class:`~firm.audit.s3_store.S3Storage` instead of SQL tables.

What S3 changes: rows are objects (see :mod:`.s3_store` for the layout and request costs), ids
come from one compare-and-swap counter per stream, maintenance runs under one lease, and
``record(conn=...)`` cannot join the caller's transaction — it stages the event in an outbox
table in the caller's database instead (committed ⇒ published by
:meth:`S3AuditLog.flush_outbox`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .._core.clock import now_utc
from .event_format import canonical, uuid_text
from .events import Reference, _ref, _validate_signed_lengths
from .integrity import canonical_created_at, new_ulid, row_mac
from .log import AnchorCallback, FindingCallback, TamperEvidentLog
from .object_store import ObjectStore, Receipt, UnknownWriteOutcome, digest, error_code
from .s3_store import (
    ROW_FIELDS,
    S3Storage,
    StorageBusy,
    decode_row,
    encode_row,
    index_values,
    row_dict,
)
from .serialization import dump_json

__all__ = ["PreparedEvent", "S3AuditLog", "StorageBusy", "UnknownEventOutcome"]

#: How many rows ``history()`` fetches per round of reads.
_HISTORY_PAGE = 100
#: Provider errors that a retry cannot fix; everything else during a write is an unknown outcome.
_NOT_RETRYABLE = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "NoSuchBucket"}


@dataclass(frozen=True)
class PreparedEvent:
    """One event, fully formed (entry id, timestamp, signature) but not yet written.

    Persist ``body`` to retry identically after a process restart: every retry of the same body
    lands on the same event, whichever process sends it.
    """

    body: str

    @property
    def entry_id(self) -> str:
        return str(json.loads(self.body)["entry_id"])


class UnknownEventOutcome(UnknownWriteOutcome):
    """The prepared event must be retried; its previous outcome is unknown."""

    def __init__(self, prepared: PreparedEvent) -> None:
        super().__init__("Audit write outcome unknown; retry exception.prepared")
        self.prepared = prepared


class S3AuditLog(TamperEvidentLog):
    def __init__(
        self,
        bucket: str,
        *,
        stream: str,
        prefix: str = "firm-audit",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
        max_age: float | None = None,
        background_retention: bool = False,
        retention_interval: float = 3600.0,
        mac_key: str | None = None,
        seal_key: str | None = None,
        background_sealing: bool = False,
        seal_interval: float = 60.0,
        grace: float = 60.0,
        seal_batch_size: int = 10_000,
        anchor_path: str | None = None,
        on_anchor: AnchorCallback | None = None,
        verify_cycle: int = 7,
        background_verification: bool = False,
        verify_interval: float = 3600.0,
        verify_full_every: int = 24,
        anchor_max_age: float | None = None,
        unsealed_tail_max_age: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_finding: FindingCallback | None = None,
        outbox_engine: Any = None,
        outbox_interval: float = 5.0,
        create_outbox: bool = True,
        workers: int = 16,
        lease_ttl: float = 300.0,
        lease_wait: float = 30.0,
    ) -> None:
        self.stream = uuid_text(stream)
        # Keys first: a bad key fails construction before any client or thread pool exists.
        self._configure_integrity(
            max_age=max_age,
            mac_key=mac_key,
            seal_key=seal_key,
            seal_interval=seal_interval,
            grace=grace,
            seal_batch_size=seal_batch_size,
            anchor_path=anchor_path,
            on_anchor=on_anchor,
            verify_cycle=verify_cycle,
            anchor_max_age=anchor_max_age,
            unsealed_tail_max_age=unsealed_tail_max_age,
        )
        self.store = ObjectStore(
            bucket, prefix=prefix, region=region, endpoint_url=endpoint_url, client=client
        )
        self.storage = S3Storage(
            self.store,
            self.stream,
            workers=workers,
            lease_ttl=lease_ttl,
            lease_wait=lease_wait,
        )
        self._start_maintenance(
            background_retention=background_retention,
            retention_interval=retention_interval,
            background_sealing=background_sealing,
            seal_interval=seal_interval,
            background_verification=background_verification,
            verify_interval=verify_interval,
            verify_full_every=verify_full_every,
            on_error=on_error,
            on_finding=on_finding,
        )
        # ``record(conn=...)`` stages events in the caller's database; with ``outbox_engine`` a
        # background relay publishes them (else call ``flush_outbox`` yourself).
        self._outbox_loop = None
        if outbox_engine is not None:
            from .s3_outbox import OutboxLoop
            from .s3_outbox import create_outbox as create_outbox_table

            if create_outbox:
                create_outbox_table(outbox_engine)
            self._outbox_loop = OutboxLoop(
                self, outbox_engine, outbox_interval, on_error=self.on_error
            )
            self._outbox_loop.start()

    # -- writing --------------------------------------------------------------------------------

    def prepare(
        self,
        action: str,
        *,
        subject: Reference = None,
        actor: Reference = None,
        data: dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> PreparedEvent:
        """Form one event exactly as ``AuditLog.record`` would store it, without writing it.

        The row's MAC does not cover its id, so it is computed here once and the id is assigned
        when the event is written.
        """
        if not isinstance(action, str):
            raise TypeError("action must be a string")
        subject_type, subject_id, subject_label = _ref(subject)
        actor_type, actor_id, actor_label = _ref(actor)
        stamp = created_at if created_at is not None else now_utc()
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        values: dict[str, Any] = {
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "subject_label": subject_label,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "actor_label": actor_label,
            "correlation_id": correlation_id,
            "data": dump_json(data),
            "changes": dump_json(changes),
            "context": dump_json(context),
        }
        # Unlike SQL, every S3 event carries an entry id: it is the write's idempotency key.
        entry_id = new_ulid(stamp)
        mac = key_id = None
        if self._key is not None:
            _validate_signed_lengths(values)
            mac = row_mac(self._key, entry_id=entry_id, created_at=stamp, **values)
            key_id = self._key.id
        body: dict[str, Any] = {
            **values,
            "entry_id": entry_id,
            "created_at": canonical_created_at(stamp),
            "row_mac": mac,
            "key_id": key_id,
        }
        if provenance is not None:
            if provenance.get("source") not in {"sql", "s3"} or not isinstance(
                provenance.get("identity"), str
            ):
                raise ValueError("invalid source provenance")
            uuid_text(provenance["stream"])
            body["provenance"] = provenance
        return PreparedEvent(canonical(body).decode())

    def write(self, prepared: PreparedEvent) -> Receipt:
        """Write a prepared event (idempotent: retries land on the same event and id)."""
        fields = _decode_prepared(prepared)
        try:
            row = self.storage.publish(fields, grace=self.grace)
        except ValueError:
            raise  # a conflict or malformed input: retrying cannot help
        except Exception as exc:
            if error_code(exc) in _NOT_RETRYABLE:
                raise  # permissions/configuration surface to the caller as-is
            # A timeout, outage or contention mid-way: the event may or may not be written, and
            # retrying this same prepared event is always safe.
            raise UnknownEventOutcome(prepared) from exc
        key = self.storage.layout.event(row["id"], row["entry_id"])
        return Receipt(key, digest(encode_row(row)))

    def record(
        self,
        action: str,
        *,
        subject: Reference = None,
        actor: Reference = None,
        data: dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> Receipt | None:
        """Record an event. Without ``conn`` it is written now.

        S3 cannot join a SQL transaction. With ``conn`` the event is staged in the outbox table
        of ``conn``'s database inside the caller's transaction (committed ⇒ it will appear;
        rolled back ⇒ it never does) and published by :meth:`flush_outbox`.
        """
        prepared = self.prepare(
            action,
            subject=subject,
            actor=actor,
            data=data,
            changes=changes,
            correlation_id=correlation_id,
            context=context,
        )
        if conn is not None:
            from .s3_outbox import stage

            stage(conn, self.stream, prepared.body)
            return None
        return self.write(prepared)

    def flush_outbox(self, engine: Any, *, limit: int = 1000) -> int:
        """Publish staged events from ``engine``'s outbox table; returns how many were written."""
        from .s3_outbox import flush

        return flush(engine, self, limit=limit)

    # -- reading --------------------------------------------------------------------------------

    def get(self, locator: int | Receipt | str) -> dict[str, Any] | None:
        """One event by id (like ``firm.audit.get``), by receipt (digest checked), or by object
        key; ``None`` when it does not exist."""
        if isinstance(locator, int):
            row = self.storage.read_row(locator)
            return row_dict(row) if row is not None else None
        try:
            body = (
                self.store.verify(locator)
                if isinstance(locator, Receipt)
                else self.store.read(locator)[0]
            )
        except FileNotFoundError:
            return None
        row = decode_row(body)
        if row is None:
            raise ValueError("object is not an audit event")
        return row_dict(row)

    def import_provenance(self, event_id: int) -> dict[str, Any] | None:
        """The original identity of a restored event (the S3 counterpart of
        :func:`firm.audit.sql_archive.import_provenance`), or ``None``."""
        row = self.storage.read_row(event_id)
        return row.get("provenance") if row is not None else None

    def history(
        self,
        *,
        subject: Reference = None,
        subject_type: str | None = None,
        subject_id: Any | None = None,
        actor: Reference = None,
        actor_type: str | None = None,
        actor_id: Any | None = None,
        action: str | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Matching events, newest first — the same filters and result shape as
        :meth:`AuditLog.history`. A filter reads its own index objects, ``since`` alone reads the
        created-at index, and the rows themselves come from the event objects."""
        if subject is not None and (subject_type is not None or subject_id is not None):
            raise ValueError("pass either subject= or subject_type=/subject_id=, not both")
        if actor is not None and (actor_type is not None or actor_id is not None):
            raise ValueError("pass either actor= or actor_type=/actor_id=, not both")
        if subject is not None:
            subject_type, subject_id, _ = _ref(subject)
        if actor is not None:
            actor_type, actor_id, _ = _ref(actor)
        wanted: dict[str, str] = {}
        for name, value in (
            ("subject_type", subject_type),
            ("subject_id", subject_id),
            ("actor_type", actor_type),
            ("actor_id", actor_id),
            ("action", action),
            ("correlation_id", correlation_id),
        ):
            if value is not None:
                wanted[name] = str(value)
        floor = None
        if since is not None:
            floor = since.astimezone(UTC).replace(tzinfo=None) if since.tzinfo else since
        if limit <= 0:
            return []
        return [row_dict(row) for row in self.matching(wanted, floor, limit)]

    def matching(
        self, wanted: dict[str, str], since: datetime | None, limit: int
    ) -> list[dict[str, Any]]:
        """Raw rows matching exact column values (and ``since``), newest id first."""
        probe = {**wanted}
        if "subject_type" in wanted and "subject_id" in wanted:
            probe["subject"] = f"{wanted['subject_type']}\x00{wanted['subject_id']}"
        if "actor_type" in wanted and "actor_id" in wanted:
            probe["actor"] = f"{wanted['actor_type']}\x00{wanted['actor_id']}"
        layout = self.storage.layout
        index = next((name for name in _INDEX_ORDER if name in probe), None)
        if index is not None:
            entries = self.storage.index_entries(layout.field(index, probe[index]))
        elif since is not None:
            # Every event since ``since``, ordered newest id first like the SQL query.
            entries = iter(sorted(self.storage.time_entries(since), reverse=True))
        else:
            entries = self.storage.index_entries(layout.id_index)
        found: list[dict[str, Any]] = []
        page: list[tuple[int, str]] = []

        def drain() -> bool:
            for row in self.storage.rows(page):
                if _matches(row, wanted, since):
                    found.append(row)
                    if len(found) >= limit:
                        return True
            return False

        for entry in entries:
            page.append(entry)
            if len(page) == _HISTORY_PAGE:
                if drain():
                    return found
                page = []
        if page:
            drain()
        return found[:limit]

    def close(self) -> None:
        if self._outbox_loop is not None:
            self._outbox_loop.stop()
        self._stop_maintenance()
        self.storage.close()
        self.store.close()

    def __enter__(self) -> S3AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


#: Index preference for ``history()``: the most selective filter that has its own index.
_INDEX_ORDER = (
    "correlation_id",
    "subject",
    "actor",
    "subject_id",
    "actor_id",
    "action",
    "subject_type",
    "actor_type",
)
if set(_INDEX_ORDER) != set(index_values({})):  # every choosable index is written per event
    raise RuntimeError("S3 history index order is out of step with the written indexes")


def _decode_prepared(prepared: PreparedEvent) -> dict[str, Any]:
    value = json.loads(prepared.body)
    if not isinstance(value, dict) or canonical(value).decode() != prepared.body:
        raise ValueError("event body must retain the canonical format produced by prepare()")
    allowed = {*ROW_FIELDS, "entry_id", "created_at", "row_mac", "key_id", "provenance"}
    if set(value) - allowed or not isinstance(value.get("action"), str):
        raise ValueError("not a prepared audit event")
    if not isinstance(value.get("entry_id"), str) or not value["entry_id"]:
        raise ValueError("prepared event has no entry id")
    datetime.fromisoformat(value["created_at"])
    return value


def _matches(row: dict[str, Any], wanted: dict[str, str], since: datetime | None) -> bool:
    if any(row.get(name) != value for name, value in wanted.items()):
        return False
    return since is None or datetime.fromisoformat(row["created_at"]) >= since
