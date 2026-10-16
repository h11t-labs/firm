"""Optional database-free audit log using immutable S3 objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from .._core.clock import now_utc
from .event_format import (
    canonical,
    read_event,
    token_decode,
    token_encode,
    utc,
    uuid_text,
    wire_event,
)
from .events import Reference, _ref
from .object_store import ObjectStore, Receipt, UnknownWriteOutcome


@dataclass(frozen=True)
class PreparedEvent:
    """Persist both fields to retry identically after a process restart."""

    key: str
    body: str


class UnknownEventOutcome(UnknownWriteOutcome):
    """The prepared event must be retried; its previous outcome is unknown."""

    def __init__(self, prepared: PreparedEvent) -> None:
        super().__init__("Audit write outcome unknown; retry exception.prepared")
        self.prepared = prepared


@dataclass(frozen=True)
class ScanPage:
    """A bounded traversal, not a snapshot or globally ordered tail."""

    items: list[dict[str, Any]]
    continuation: str | None
    exhausted: bool
    scanned: int
    requests: int


class S3AuditLog:
    def __init__(
        self,
        bucket: str,
        *,
        stream: str,
        prefix: str = "firm-audit",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        self.stream = uuid_text(stream)
        self.store = ObjectStore(
            bucket, prefix=prefix, region=region, endpoint_url=endpoint_url, client=client
        )
        self.event_prefix = f"{self.store.prefix}events/v1/{self.stream}/"

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
        event_id: str | None = None,
        created_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> PreparedEvent:
        if not isinstance(action, str):
            raise TypeError("action must be a string")
        ident = uuid_text(event_id) if event_id is not None else str(uuid4())
        timestamp = utc(created_at if created_at is not None else now_utc())
        st, si, sl = _ref(subject)
        at, ai, al = _ref(actor)
        event = wire_event(
            {
                "action": action,
                "subject_type": st,
                "subject_id": si,
                "subject_label": sl,
                "actor_type": at,
                "actor_id": ai,
                "actor_label": al,
                "data": data,
                "changes": changes,
                "context": context,
                "correlation_id": correlation_id,
                "created_at": timestamp,
            }
        )
        envelope = {"v": 1, "stream": self.stream, "id": ident, "event": event}
        if provenance is not None:
            if provenance.get("source") not in {"sql", "s3"} or not isinstance(
                provenance.get("identity"), str
            ):
                raise ValueError("invalid source provenance")
            uuid_text(provenance["stream"])
            envelope["provenance"] = provenance
        return PreparedEvent(
            f"{self.event_prefix}{timestamp.date().isoformat()}/{ident}.json",
            canonical(envelope).decode(),
        )

    def _decode(self, key: str, body: bytes) -> dict[str, Any]:
        value = json.loads(body)
        if canonical(value) != body:
            raise ValueError("event body must retain the canonical format produced by prepare()")
        if value.get("v") != 1 or value.get("stream") != self.stream:
            raise ValueError("unsupported event format or stream")
        event = read_event(value["event"])
        ident = uuid_text(value["id"])
        expected = f"{self.event_prefix}{event['created_at'].date().isoformat()}/{ident}.json"
        if key != expected:
            raise ValueError("event content does not match its locator")
        return {
            **event,
            "id": ident,
            "key": key,
            "stream": self.stream,
            "provenance": value.get("provenance"),
        }

    def write(self, prepared: PreparedEvent) -> Receipt:
        self._decode(prepared.key, prepared.body.encode())
        try:
            return self.store.put(prepared.key, prepared.body.encode())
        except UnknownWriteOutcome as exc:
            raise UnknownEventOutcome(prepared) from exc

    def record(self, action: str, *, conn: Any = None, **kwargs: Any) -> Receipt:
        if conn is not None:
            raise ValueError("S3 audit writes cannot join a SQL transaction; use AuditLog")
        return self.write(self.prepare(action, **kwargs))

    def get(self, locator: Receipt | str) -> dict[str, Any]:
        if isinstance(locator, Receipt):
            body = self.store.verify(locator)
            key = locator.key
        else:
            key = locator
            body, _ = self.store.read(key)
        return self._decode(key, body)

    def scan(
        self,
        *,
        start: date,
        end: date,
        limit: int = 100,
        max_objects: int = 1000,
        max_requests: int = 1100,
        continuation: str | None = None,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ScanPage:
        """Traverse [start, end) UTC date partitions in ascending key order.

        Filters never change scan budgets. A fresh scan may be needed for concurrent/backdated
        insertions. Changing budgets/limit when resuming is allowed; changing filters is not.
        """
        page, _ = self._scan(
            start=start,
            end=end,
            limit=limit,
            max_objects=max_objects,
            max_requests=max_requests,
            continuation=continuation,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        return page

    def _scan(
        self,
        *,
        start: date,
        end: date,
        limit: int,
        max_objects: int,
        max_requests: int,
        continuation: str | None,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ScanPage, dict[str, tuple[bytes, str | None]]]:
        """``scan`` plus each returned item's stored bytes and version, keyed by object key, so
        an exporter can copy exactly what it already read instead of fetching it again."""
        if start >= end or (end - start).days > 366:
            raise ValueError("scan requires a nonempty date range of at most 366 days")
        if (
            not 1 <= limit <= 1000
            or not 1 <= max_objects <= 10000
            or not 2 <= max_requests <= 20000
        ):
            raise ValueError("invalid scan limit or budget")
        filters = {
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "correlation_id": correlation_id,
        }
        query = [self.store.identity, self.stream, start.isoformat(), end.isoformat(), filters]
        day, after = start, ""
        if continuation:
            position = token_decode(continuation, query)
            if not isinstance(position, list) or len(position) != 2:
                raise ValueError("invalid continuation position")
            try:
                day, after = date.fromisoformat(position[0]), position[1]
                if not start <= day < end or not isinstance(after, str):
                    raise ValueError("invalid continuation position")
            except (TypeError, IndexError) as exc:
                raise ValueError("invalid continuation position") from exc
        items: list[dict[str, Any]] = []
        stored: dict[str, tuple[bytes, str | None]] = {}
        scanned = requests = 0
        while day < end and scanned < max_objects and requests + 2 <= max_requests:
            prefix = f"{self.event_prefix}{day.isoformat()}/"
            keys, complete = self.store.list_keys(
                prefix, after=after, limit=min(1000, max_objects - scanned)
            )
            requests += 1
            consumed = 0
            for key in keys:
                if requests >= max_requests or scanned >= max_objects or len(items) >= limit:
                    break
                body, version = self.store.read(key)
                event = self._decode(key, body)
                scanned += 1
                requests += 1
                consumed += 1
                after = key
                if all(
                    value is None or event[field] == str(value) for field, value in filters.items()
                ):
                    items.append(event)
                    stored[key] = (body, version)
            if consumed == len(keys) and complete:
                day, after = day + timedelta(days=1), ""
            if len(items) >= limit:
                break
        exhausted = day >= end
        token = None if exhausted else token_encode(query, [day.isoformat(), after])
        return ScanPage(items, token, exhausted, scanned, requests), stored

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> S3AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
