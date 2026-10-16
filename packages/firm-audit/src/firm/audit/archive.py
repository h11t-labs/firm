"""Immutable audit archives: staged input, data, then verified completion manifest."""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import asdict
from datetime import date
from typing import Any
from uuid import uuid4

from .event_format import canonical, read_event, token_decode, token_encode, uuid_text, wire_event
from .object_store import MAX_OBJECT_BYTES, ObjectConflict, ObjectStore, Receipt, digest
from .s3 import S3AuditLog

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
                    entry["source"] != "sql"
                    or not isinstance(integrity, dict)
                    or set(integrity) != {"entry_id", "row_mac", "key_id"}
                    or not all(
                        value is None or isinstance(value, str) for value in integrity.values()
                    )
                ):
                    raise ValueError("invalid SQL integrity metadata")
            if entry["source"] == "sql":
                expected = f"{entry['original_id']}:{digest(canonical(entry['event']))}"
                if entry["identity"] != expected or not isinstance(entry["original_id"], int):
                    raise ValueError("invalid SQL archive identity")
            else:
                receipt = Receipt(**entry["receipt"])
                if entry["identity"] != receipt.key:
                    raise ValueError("invalid S3 archive identity")
                envelope = entry.get("envelope")
                if envelope is not None and (
                    envelope.get("v") != 1
                    or envelope.get("stream") != entry["stream"]
                    or digest(canonical({**envelope, "event": entry["event"]})) != receipt.sha256
                ):
                    raise ObjectConflict("S3 source envelope does not match its receipt")

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
        source: S3AuditLog,
        *,
        start: date,
        end: date,
        batch_id: str,
        continuation: str | None = None,
        max_objects: int = 100,
    ) -> Receipt:
        """Copy one bounded selection. scope.continuation resumes the source traversal.

        Reuse the batch ID after interruption. Concurrent LIST is explicitly not a snapshot.
        """
        base = self._base(batch_id)
        query = {
            "source": source.store.identity,
            "stream": source.stream,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "continuation": continuation,
        }
        try:
            frozen, _ = self.store.read(base + "input.json")
        except FileNotFoundError:
            frozen = None
        if frozen is not None:
            if json.loads(frozen)["scope"]["query"] != query:
                raise ObjectConflict("batch belongs to another export query")
            return self.resume(batch_id)
        page, stored = source._scan(
            start=start,
            end=end,
            limit=1000,
            max_objects=max_objects,
            continuation=continuation,
            max_requests=min(20000, max_objects + 1000),
        )
        entries = []
        for row in page.items:
            # The scan already read and decoded each object; its decoder proved the stored body
            # is canonical and matches its key, so these bytes are the envelope to copy.
            raw, version = stored[row["key"]]
            envelope = json.loads(raw)
            entries.append(
                {
                    "source": "s3",
                    "stream": source.stream,
                    "identity": row["key"],
                    "event": envelope.pop("event"),
                    "envelope": envelope,
                    "receipt": asdict(Receipt(row["key"], digest(raw), version)),
                }
            )
        return self.publish(
            entries,
            batch_id=batch_id,
            scope={
                "kind": "observed-selection",
                "query": query,
                "continuation": page.continuation,
                "traversal_exhausted": page.exhausted,
            },
        )

    def restore_s3(self, locator: Receipt | str, target: S3AuditLog) -> list[Receipt]:
        """Restore S3 identities or map SQL identities with retained provenance."""
        from uuid import UUID, uuid5

        from .events import Ref
        from .s3 import PreparedEvent

        entries = self.read(locator)
        if any(e["source"] == "s3" and e["stream"] != target.stream for e in entries):
            raise ValueError("S3-source restore requires its original target stream")
        restored = []
        for entry in entries:
            row = read_event(entry["event"])
            if entry["source"] == "sql":
                prepared = target.prepare(
                    row["action"],
                    subject=Ref(row["subject_type"], row["subject_id"], row["subject_label"]),
                    actor=Ref(row["actor_type"], row["actor_id"], row["actor_label"]),
                    data=row["data"],
                    changes=row["changes"],
                    context=row["context"],
                    correlation_id=row["correlation_id"],
                    created_at=row["created_at"],
                    event_id=str(uuid5(UUID(entry["stream"]), entry["identity"])),
                    provenance=provenance(entry),
                )
            else:
                ident = entry["identity"].rsplit("/", 1)[-1].removesuffix(".json")
                partition = row["created_at"].date().isoformat()
                key = f"{target.event_prefix}{partition}/{uuid_text(ident)}.json"
                header = entry.get("envelope") or {"v": 1, "stream": target.stream, "id": ident}
                body = canonical({**header, "event": entry["event"]})
                if digest(body) != entry["receipt"]["sha256"]:
                    raise ObjectConflict("restored event differs from source digest")
                prepared = PreparedEvent(key, body.decode())
            restored.append(target.write(prepared))
        return restored

    def close(self) -> None:
        self.store.close()
