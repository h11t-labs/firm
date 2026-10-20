"""Durable SQL archive discovery and exact, verified pruning eligibility."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from sqlalchemy import Connection, and_, inspect, select

from .._core.dialects import get_dialect
from . import events, schema
from .archive import Archive, provenance, sql_entry
from .event_format import canonical, uuid_text
from .log import AuditLog
from .object_store import ObjectConflict, Receipt, digest


def lock_policy(conn: Connection, *, schema_present: bool | None = None) -> Any:
    """Serialize policy activation, selection, and pruning on one tiny coordination row.

    ``schema_present`` lets a caller that opens one transaction per batch inspect the schema
    once instead of on every iteration; ``None`` inspects here.
    """
    if schema_present is None:
        schema_present = inspect(conn).has_table(schema.archive_policy.name)
    if not schema_present:
        return None  # An existing pre-archive deployment remains supported.
    policy = conn.execute(
        select(schema.archive_policy).where(schema.archive_policy.c.id == "1").with_for_update()
    ).first()
    if policy is None:
        raise ValueError("archive policy row missing; repair/upgrade the audit schema")
    return policy


def fingerprint(entry: dict[str, Any]) -> str:
    return digest(canonical(entry))


def import_provenance(conn: Connection, event_id: int) -> dict[str, Any] | None:
    """Read original identity for an imported event without changing the SQL event surface."""
    value = conn.execute(
        select(schema.archive_imports.c.provenance).where(
            schema.archive_imports.c.event_id == event_id
        )
    ).scalar_one_or_none()
    return json.loads(value) if value is not None else None


def entry_for(stream: str, row: Any, provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Build a stored row's archive entry; ``provenance`` is its import record, if any."""
    event = events._row_to_dict(row)
    if provenance is not None:
        event["provenance"] = provenance
    entry = sql_entry(stream, event)
    # MACs cover the stored JSON strings, including their whitespace/order. Never re-encode
    # these bytes when copying a SQL row, even if they decode to the same Python payload.
    for field in ("data", "changes", "context"):
        entry["event"][field] = row._mapping[field]
    entry["identity"] = f"{row.id}:{digest(canonical(entry['event']))}"
    signed = {name: row._mapping[name] for name in ("entry_id", "row_mac", "key_id")}
    if any(value is not None for value in signed.values()):
        entry["integrity"] = signed
    return entry


def source_entry(
    conn: Connection, stream: str, row: Any, *, include_import_metadata: bool = True
) -> dict[str, Any]:
    provenance = import_provenance(conn, row.id) if include_import_metadata else None
    return entry_for(stream, row, provenance)


def verified_ids(
    conn: Connection, stream: str, rows: Sequence[Any], *, span: tuple[int, int] | None = None
) -> set[int]:
    """IDs among ``rows`` whose exact content sits in a verified archive batch, in one query.

    Matches content, not just a potentially reused SQLite integer ID: the stored fingerprint
    covers the row bytes, its signature columns, and any import provenance, so it is recomputed
    here from the same inputs. ``span`` selects the members in ``(low, high]`` by range (a whole
    seal range); otherwise the rows' IDs are listed.
    """
    if not rows:
        return set()
    member, batches, imports = (
        schema.archive_members,
        schema.archive_batches,
        schema.archive_imports,
    )
    predicate = (
        and_(member.c.event_id > span[0], member.c.event_id <= span[1])
        if span is not None
        else member.c.event_id.in_([row.id for row in rows])
    )
    archived = {
        event_id: (stored, json.loads(raw) if raw is not None else None)
        for event_id, stored, raw in conn.execute(
            select(member.c.event_id, member.c.fingerprint, imports.c.provenance)
            .select_from(
                member.join(batches, member.c.batch_id == batches.c.id).outerjoin(
                    imports, imports.c.event_id == member.c.event_id
                )
            )
            .where(predicate, batches.c.receipt.is_not(None))
        )
    }
    return {
        row.id
        for row in rows
        if row.id in archived
        and fingerprint(entry_for(stream, row, archived[row.id][1])) == archived[row.id][0]
    }


class Archiver:
    """Continuous archive-before-prune for any audit log, SQL or S3.

    The persisted policy covers module-level and instance writes, including delayed commits.
    Upgrade every pruner and pause pruning during policy activation/backfill. No callbacks are
    needed on the record path: stored rows themselves are the durable source. An S3 log archives
    under its own stream; a SQL log names one.
    """

    def __init__(self, audit: Any, archive: Archive, *, stream: str | None = None) -> None:
        own = audit.storage.stream
        if stream is None and own is None:
            raise ValueError("a SQL audit log needs an archive stream")
        chosen = uuid_text(str(stream if stream is not None else own))
        if own is not None and chosen != own:
            raise ValueError("an S3 audit log archives under its own stream")
        self.audit = audit
        self.archive = archive
        self.stream = chosen

    def enable(self) -> None:
        """Opt in to archive-before-prune; binds permanently to one stream/destination."""
        with self.audit.storage.claim() as conn:
            policy = conn.archive_policy()
            if policy is None:
                raise ValueError("Upgrade the audit schema before enabling archives")
            if policy.stream and (
                policy.stream != self.stream or policy.destination != self.archive.store.identity
            ):
                raise ValueError("archive policy already belongs to another stream/destination")
            conn.set_archive_policy(self.stream, self.archive.store.identity)

    def _require_policy(self, policy: Any) -> None:
        if (
            policy is None
            or policy.stream != self.stream
            or policy.destination != self.archive.store.identity
        ):
            raise ValueError("Call enable() with this stream/destination before exporting")

    def run_once(self, *, batch_size: int = 100) -> Receipt | None:
        """Publish one frozen batch; concurrent workers may safely retry identical work.

        Immutable batch membership never changes, so competing workers publish the same
        bytes. Discovery excludes reserved IDs without a commit-order cursor.
        """
        from uuid import uuid4

        if not 1 <= batch_size <= 1000:
            raise ValueError("invalid batch/discovery limits")
        with self.audit.storage.claim() as conn:
            self._require_policy(conn.archive_policy())
            pending = conn.pending_archive_batch()
            if pending is not None:
                batch_id, body = pending
                if body is None:
                    raise ValueError("pending archive batch has no frozen input")
            else:
                entries = [
                    conn.archive_entry(self.stream, row) for row in conn.unreserved_rows(batch_size)
                ]
                if not entries:
                    return None
                batch_id = str(uuid4())
                body = canonical(entries).decode()
                # Reject oversized input before committing an unrecoverable pending batch.
                if len(body.encode()) > 8 * 1024 * 1024:
                    raise ValueError("batch exceeds 8 MiB; reduce batch_size")
                conn.reserve_archive_batch(
                    batch_id,
                    body,
                    [
                        {
                            "fingerprint": fingerprint(entry),
                            "event_id": entry["original_id"],
                            "batch_id": batch_id,
                        }
                        for entry in entries
                    ],
                )
        entries = json.loads(body)
        receipt = self.archive.publish(
            entries,
            batch_id=batch_id,
            scope={"kind": "exact-sql-membership", "stream": self.stream},
        )
        verified = self.archive.read(receipt)
        if canonical(verified) != canonical(entries):
            raise ObjectConflict("verified archive does not match reserved batch")
        self.audit.storage.acknowledge_archive_batch(batch_id, canonical(asdict(receipt)).decode())
        return receipt

    def status(self) -> dict[str, int]:
        return self.audit.storage.archive_status()


#: The original name; it archives S3 logs too.
SQLArchiver = Archiver


def restore_sql(archive: Archive, receipts: list[Receipt | str], target: AuditLog) -> int:
    """Restore complete SQL archives into an empty target, atomically; dedupe overlaps."""
    entries = archive.read_many(receipts)
    native_sql = all(entry["source"] == "sql" for entry in entries)
    if native_sql and len({e["stream"] for e in entries}) > 1:
        raise ValueError("restore one SQL stream into each empty target")
    ids = [e["original_id"] for e in entries] if native_sql else []
    if len(ids) != len(set(ids)):
        raise ValueError("source reused SQL IDs; restore these batches into separate targets")
    with get_dialect(target.engine).begin_claim_tx(target.engine) as conn:
        if conn.execute(select(schema.audit_events.c.id).limit(1)).first() is not None:
            raise ValueError("restore requires an empty target")
        if conn.execute(select(schema.archive_imports.c.event_id).limit(1)).first() is not None:
            raise ValueError("restore requires an isolated target without old import metadata")
        if conn.execute(select(schema.seals.c.id).limit(1)).first() is not None:
            raise ValueError("restore requires an isolated target without old integrity records")
        if conn.execute(select(schema.verify_status.c.id).limit(1)).first() is not None:
            raise ValueError("restore requires an isolated target without old verification state")
        from datetime import datetime

        for entry in entries:
            event = dict(entry["event"])
            event["created_at"] = datetime.fromisoformat(event["created_at"]).replace(tzinfo=None)
            if native_sql:
                event["id"] = entry["original_id"]
                event.update(entry.get("integrity", {}))
            result = conn.execute(schema.audit_events.insert().values(**event))
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise ValueError("provider did not return the restored event identity")
            ident = primary_key[0]
            origin = entry.get("envelope", {}).get("provenance") or provenance(entry)
            conn.execute(
                schema.archive_imports.insert().values(
                    event_id=ident, provenance=canonical(origin).decode()
                )
            )
        if entries:
            get_dialect(target.engine).reset_identity(conn, schema.audit_events)
    return len(entries)
