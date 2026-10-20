"""Immutable audit archives: staged input, data, then verified completion manifest."""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import uuid4

from .event_format import canonical, read_event, token_decode, token_encode, uuid_text, wire_event
from .log import AuditLog
from .object_store import (
    MAX_OBJECT_BYTES,
    ObjectConflict,
    ObjectStore,
    ObjectUnavailable,
    Receipt,
    digest,
)
from .s3_store import S3Storage

MAX_ARCHIVE_RECORDS = 1000


def sql_entry(stream: str, row: dict[str, Any]) -> dict[str, Any]:
    event = wire_event(row)
    # SQLite may reuse an integer after every row is pruned. Include the immutable row's
    # fingerprint so a later incarnation cannot inherit an old archive acknowledgement.
    identity = f"{row['id']}:{digest(canonical(event))}"
    result = {
        "source": "sql",
        "stream": stream,
        "identity": identity,
        "original_id": row["id"],
        "event": event,
    }
    if row.get("provenance") is not None:
        result["provenance"] = row["provenance"]
    return result


def row_entry(source: str, stream: str, row: dict[str, Any]) -> dict[str, Any]:
    """An archive entry for a stored row in SQL-column shape (an S3 event object): the stored
    JSON strings verbatim (row MACs cover their exact bytes) plus any signature columns."""
    from .s3_store import row_dict

    event = row_dict(row)
    if row.get("provenance") is not None:
        event["provenance"] = row["provenance"]
    entry = sql_entry(stream, event)
    entry["source"] = source
    for field in ("data", "changes", "context"):
        entry["event"][field] = row.get(field)
    entry["identity"] = f"{row['id']}:{digest(canonical(entry['event']))}"
    signed = {name: row.get(name) for name in ("entry_id", "row_mac", "key_id")}
    if any(value is not None for value in signed.values()):
        entry["integrity"] = signed
    return entry


def _native_row(entry: dict[str, Any]) -> dict[str, Any]:
    """An archived S3 row back in storage shape, with its original id and signature."""
    from .integrity import canonical_created_at
    from .s3_store import ROW_FIELDS

    event = entry["event"]
    created = datetime.fromisoformat(event["created_at"])
    row = {name: event.get(name) for name in ROW_FIELDS}
    row.update(
        id=entry["original_id"],
        created_at=canonical_created_at(created),
        **(entry.get("integrity") or {}),
    )
    if event.get("provenance") is not None:
        row["provenance"] = event["provenance"]
    return row


def provenance(entry: dict[str, Any]) -> dict[str, Any]:
    """Retain the original source identity through repeated backend conversions."""
    return entry.get("provenance") or {
        "source": entry["source"],
        "stream": entry["stream"],
        "identity": entry["identity"],
        "original_id": entry.get("original_id"),
        **({"sql_integrity": entry["integrity"]} if entry.get("integrity") else {}),
    }


def entry_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return entry["source"], entry["stream"], entry["identity"]


def signed_count(entries: list[dict[str, Any]]) -> int:
    """Entries carrying a row signature (the MAC recorded when the row was written)."""
    return sum(1 for entry in entries if (entry.get("integrity") or {}).get("row_mac"))


def _signing_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Manifest fields that let a listing show signing without downloading batch data."""
    return {"signed": signed_count(entries), "sources": sorted({e["source"] for e in entries})}


class Archive:
    """Copy-only S3 archive with explicit verification and restoration."""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store
        self.prefix = f"{store.prefix}archives/v1/"

    def _base(self, batch_id: str) -> str:
        return f"{self.prefix}{uuid_text(batch_id)}/"

    def publish(
        self,
        entries: list[dict[str, Any]],
        *,
        batch_id: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> Receipt:
        """Freeze selection before publication. Retry with the same batch or call resume()."""
        ident = uuid_text(batch_id) if batch_id else str(uuid4())
        self._validate_entries(entries)
        staging = canonical({"v": 1, "entries": entries, "scope": scope or {}})
        self.store.put(self._base(ident) + "input.json", staging)
        return self.resume(ident)

    @staticmethod
    def _validate_entries(entries: list[dict[str, Any]]) -> None:
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ARCHIVE_RECORDS:
            raise ValueError("archive requires 1..1000 events")
        seen: set[tuple[str, str, str]] = set()
        for entry in entries:
            if entry["source"] not in {"sql", "s3"}:
                raise ValueError("unknown archive source")
            uuid_text(entry["stream"])
            read_event(entry["event"])
            key = entry_key(entry)
            if not isinstance(key[2], str) or key in seen:
                raise ValueError("duplicate or invalid archive identity")
            seen.add(key)
            if entry.get("integrity") is not None:
                integrity = entry["integrity"]
                if (
                    not isinstance(integrity, dict)
                    or set(integrity) != {"entry_id", "row_mac", "key_id"}
                    or not all(
                        value is None or isinstance(value, str) for value in integrity.values()
                    )
                ):
                    raise ValueError("invalid row integrity metadata")
            # SQL and S3 rows share one entry shape: the id plus a fingerprint of the exact row.
            expected = f"{entry.get('original_id')}:{digest(canonical(entry['event']))}"
            if entry["identity"] != expected or not isinstance(entry["original_id"], int):
                raise ValueError("invalid archive identity")

    def resume(self, batch_id: str) -> Receipt:
        base = self._base(batch_id)
        data, _ = self.store.read(base + "input.json")
        staging = json.loads(data)
        if staging.get("v") != 1:
            raise ValueError("unsupported archive input format")
        entries = staging["entries"]
        self._validate_entries(entries)
        raw = b"".join(canonical(entry) + b"\n" for entry in entries)
        if len(raw) > MAX_OBJECT_BYTES:
            raise ValueError("archive decompressed size exceeds limit; use a smaller batch")
        blob = gzip.compress(raw, mtime=0)
        payload = self.store.put(base + "data.jsonl.gz", blob, content_type="application/gzip")
        self.store.verify(payload)
        timestamps = [read_event(entry["event"])["created_at"] for entry in entries]
        manifest = {
            "min_created_at": min(timestamps).isoformat(),
            "max_created_at": max(timestamps).isoformat(),
            "v": 1,
            "batch_id": uuid_text(batch_id),
            "count": len(entries),
            "data": asdict(payload),
            "scope": staging["scope"],
            "sha256": digest(raw),
            **_signing_summary(entries),
        }
        receipt = self.store.put(base + "manifest.json", canonical(manifest))
        self.read(receipt)
        return receipt

    def read(self, locator: Receipt | str) -> list[dict[str, Any]]:
        """Verify a completed batch and return lossless source envelopes."""
        return self._load(locator)[1]

    def inspect(self, locator: Receipt | str) -> dict[str, Any]:
        """Verify and expose manifest scope/progress for the next bounded export."""
        return self._load(locator)[0]

    def batches(
        self, *, limit: int = 25, continuation: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List batches in batch-id (key) order, not creation order, with manifest summaries.

        Bounded: one LIST plus one manifest GET per published batch. Summaries (count, time range,
        ``signed`` rows, ``sources``) are read, not verified; ``read()`` verifies a batch. A batch
        with staged input but no manifest is ``pending`` (``resume()`` completes it); a manifest
        that does not parse is ``unreadable``. Returns the page and a continuation for the next
        one, or ``None``.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        query = {"archive": self.prefix}
        after = token_decode(continuation, query) if continuation else ""
        keys, exhausted = self.store.list_keys(self.prefix, after=after, limit=limit * 3 + 3)
        groups: dict[str, list[str]] = {}
        for key in keys:
            groups.setdefault(key[len(self.prefix) :].split("/", 1)[0], []).append(key)
        batch_ids = list(groups)
        if not exhausted and len(batch_ids) > 1:
            batch_ids.pop()  # its keys may continue on the next LIST page
        batch_ids = batch_ids[:limit]
        items = [self._batch_summary(batch_id, groups[batch_id]) for batch_id in batch_ids]
        more = not exhausted or len(groups) > len(batch_ids)
        token = token_encode(query, groups[batch_ids[-1]][-1]) if more and batch_ids else None
        return items, token

    def _batch_summary(self, batch_id: str, keys: list[str]) -> dict[str, Any]:
        manifest_key = f"{self.prefix}{batch_id}/manifest.json"
        summary: dict[str, Any] = {"batch_id": batch_id, "manifest_key": None, "status": "pending"}
        if manifest_key not in keys:
            return summary
        summary["manifest_key"] = manifest_key
        try:
            manifest = json.loads(self.store.read(manifest_key)[0])
            summary.update(
                status="complete",
                count=int(manifest["count"]),
                min_created_at=manifest["min_created_at"],
                max_created_at=manifest["max_created_at"],
                signed=manifest.get("signed"),
                sources=manifest.get("sources"),
            )
        except (ValueError, KeyError, TypeError, FileNotFoundError, ObjectUnavailable):
            summary["status"] = "unreadable"
        return summary

    def read_many(
        self,
        locators: list[Receipt | str],
        *,
        max_events: int = 10000,
    ) -> list[dict[str, Any]]:
        """Deduplicate overlapping batches; reject content conflicts by source identity."""
        if not 1 <= len(locators) <= 100 or not 1 <= max_events <= 100000:
            raise ValueError("invalid archive read budget")
        entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        for locator in locators:
            for entry in self.read(locator):
                identity = entry_key(entry)
                previous = entries.get(identity)
                if previous is not None and (
                    previous["event"] != entry["event"]
                    or previous.get("envelope") != entry.get("envelope")
                    or provenance(previous) != provenance(entry)
                    or previous.get("integrity") != entry.get("integrity")
                ):
                    raise ObjectConflict("conflicting archive source identity")
                entries.setdefault(identity, entry)
                if len(entries) > max_events:
                    raise ValueError("archive read exceeds event budget")
        return list(entries.values())

    def _load(self, locator: Receipt | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        key = locator.key if isinstance(locator, Receipt) else locator
        if not key.startswith(self.prefix) or not key.endswith("/manifest.json"):
            raise ValueError("not an archive manifest locator")
        body = (
            self.store.verify(locator) if isinstance(locator, Receipt) else self.store.read(key)[0]
        )
        manifest = json.loads(body)
        if manifest.get("v") != 1 or key != self._base(manifest["batch_id"]) + "manifest.json":
            raise ValueError("unsupported or misplaced archive manifest")
        receipt = Receipt(**manifest["data"])
        if receipt.key != self._base(manifest["batch_id"]) + "data.jsonl.gz":
            raise ValueError("archive data locator is outside batch")
        compressed = self.store.verify(receipt)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
            raw = handle.read(MAX_OBJECT_BYTES + 1)
        if len(raw) > MAX_OBJECT_BYTES or digest(raw) != manifest["sha256"]:
            raise ObjectConflict("archive content digest or size mismatch")
        entries = [json.loads(line) for line in raw.splitlines()]
        self._validate_entries(entries)
        if len(entries) != manifest["count"]:
            raise ObjectConflict("archive count mismatch")
        summary = _signing_summary(entries)
        if any(field in manifest and manifest[field] != value for field, value in summary.items()):
            raise ObjectConflict("archive signing summary does not match its data")
        return manifest, entries

    def export_sql(
        self,
        source: Any,
        *,
        stream: str,
        batch_id: str,
        limit: int = 100,
        since: Any = None,
        until: Any = None,
        continuation: str | None = None,
    ) -> Receipt:
        """Copy a bounded SQL selection, retaining source rows and existing pruning policy.

        Reusing batch_id resumes its frozen selection. Coordinate source pruning separately
        when exporting a historical scope; a bounded selection is not a full database backup.
        """
        from sqlalchemy import inspect, select

        from .._core.database import transaction
        from . import schema
        from .sql_archive import source_entry

        stream = uuid_text(stream)
        if not 1 <= limit <= MAX_ARCHIVE_RECORDS:
            raise ValueError("invalid SQL export limit")
        query = {
            "stream": stream,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        }
        after = token_decode(continuation, query) if continuation else 0
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError("invalid SQL export continuation")
        request = {**query, "limit": limit, "continuation": continuation}
        try:
            frozen, _ = self.store.read(self._base(batch_id) + "input.json")
        except FileNotFoundError:
            frozen = None
        if frozen is not None:
            if json.loads(frozen)["scope"]["query"] != request:
                raise ObjectConflict("batch belongs to another export query")
            return self.resume(batch_id)
        stmt = (
            select(schema.audit_events)
            .where(schema.audit_events.c.id > after)
            .order_by(schema.audit_events.c.id)
            .limit(limit + 1)
        )
        if since is not None:
            stmt = stmt.where(schema.audit_events.c.created_at >= since)
        if until is not None:
            stmt = stmt.where(schema.audit_events.c.created_at < until)
        with transaction(source.engine) as conn:
            selected = list(conn.execute(stmt))
            has_imports = inspect(conn).has_table(schema.archive_imports.name)
            entries = [
                source_entry(conn, stream, row, include_import_metadata=has_imports)
                for row in selected[:limit]
            ]
        exhausted = len(selected) <= limit
        next_token = None if exhausted else token_encode(query, entries[-1]["original_id"])
        return self.publish(
            entries,
            batch_id=batch_id,
            scope={
                "kind": "bounded-sql-selection",
                "query": request,
                "continuation": next_token,
                "traversal_exhausted": exhausted,
            },
        )

    def export_s3(
        self,
        source: AuditLog,
        *,
        batch_id: str,
        limit: int = 100,
        since: Any = None,
        until: Any = None,
        continuation: str | None = None,
    ) -> Receipt:
        """Copy a bounded selection of an S3 audit log by id, exactly like :meth:`export_sql`
        (same entry shape, same continuation and resume rules)."""
        if not 1 <= limit <= MAX_ARCHIVE_RECORDS:
            raise ValueError("invalid S3 export limit")
        storage = source.storage
        if not isinstance(storage, S3Storage):
            raise ValueError("export_s3 requires S3Storage")
        query = {
            "source": storage.store.identity,
            "stream": storage.stream,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        }
        after = token_decode(continuation, query) if continuation else 0
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError("invalid S3 export continuation")
        request = {**query, "limit": limit, "continuation": continuation}
        try:
            frozen, _ = self.store.read(self._base(batch_id) + "input.json")
        except FileNotFoundError:
            frozen = None
        if frozen is not None:
            if json.loads(frozen)["scope"]["query"] != request:
                raise ObjectConflict("batch belongs to another export query")
            return self.resume(batch_id)
        selected = storage.rows_after(after, limit + 1, since=since, until=until)
        entries = [row_entry("s3", storage.stream, row) for row in selected[:limit]]
        exhausted = len(selected) <= limit
        next_token = None if exhausted else token_encode(query, entries[-1]["original_id"])
        if not entries:
            raise ValueError("export selection is empty")
        return self.publish(
            entries,
            batch_id=batch_id,
            scope={
                "kind": "bounded-s3-selection",
                "query": request,
                "continuation": next_token,
                "traversal_exhausted": exhausted,
            },
        )

    def restore_s3(self, locators: Receipt | str | list[Receipt | str], target: AuditLog) -> int:
        """Restore archives into an empty S3 audit log, like :func:`~.sql_archive.restore_sql`.

        Archives of the target's own stream keep their original ids and row signatures (so they
        still verify under the original key); anything else gets fresh ids, is signed by the
        target, and keeps its original identity as provenance.
        """
        receipts = locators if isinstance(locators, list) else [locators]
        entries = self.read_many(receipts)
        storage = target.storage
        if not isinstance(storage, S3Storage):
            raise ValueError("restore_s3 requires S3Storage")
        native = all(e["source"] == "s3" and e["stream"] == storage.stream for e in entries)
        ids = [e["original_id"] for e in entries] if native else []
        if len(ids) != len(set(ids)):
            raise ValueError("source reused ids; restore these batches into separate targets")
        if not storage.is_empty():
            raise ValueError("restore requires an empty target stream")
        if native:
            storage.restore_rows([_native_row(entry) for entry in entries])
            return len(entries)
        from .events import Ref

        for entry in entries:
            row = read_event(entry["event"])
            target.write(
                target.prepare(
                    row["action"],
                    subject=Ref(row["subject_type"], row["subject_id"], row["subject_label"]),
                    actor=Ref(row["actor_type"], row["actor_id"], row["actor_label"]),
                    data=row["data"],
                    changes=row["changes"],
                    context=row["context"],
                    correlation_id=row["correlation_id"],
                    created_at=row["created_at"],
                    provenance=provenance(entry),
                )
            )
        return len(entries)

    def close(self) -> None:
        self.store.close()
