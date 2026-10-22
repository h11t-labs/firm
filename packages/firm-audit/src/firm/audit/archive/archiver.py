"""Continuous archive-before-prune policy shared by all audit storage backends."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..event_format import canonical, uuid_text
from ..object_store import ObjectConflict, Receipt
from .store import Archive, fingerprint


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
