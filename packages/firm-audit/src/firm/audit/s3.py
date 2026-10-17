"""Optional database-free audit log using immutable S3 objects."""

from __future__ import annotations

import json
import os
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
from .integrity import HmacSigner, Key, add_key, load_key, parse_keyring, s3_event_mac
from .object_store import ObjectConflict, ObjectStore, Receipt, UnknownWriteOutcome

_MAC_HEX_LENGTH = 64


@dataclass(frozen=True)
class PreparedEvent:
    """Persist both fields to retry identically after a process restart."""

    key: str
    body: str


class EventSignatureError(ObjectConflict):
    """An S3 event's signature does not verify or is malformed, or a required one is missing."""


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
        mac_key: str | None = None,
        require_signatures: bool = False,
    ) -> None:
        self.stream = uuid_text(stream)
        # The same key rules as AuditLog: ``mac_key`` wins, else FIRM_AUDIT_KEY signs new events;
        # it and FIRM_AUDIT_RETIRED_KEYS verify what is read. Loaded before the client exists so
        # a bad key fails construction without leaking one.
        self._key = load_key(mac_key if mac_key is not None else os.environ.get("FIRM_AUDIT_KEY"))
        self._keyring: dict[str, Key] = {}
        if self._key is not None:
            add_key(self._keyring, self._key, source="FIRM_AUDIT_KEY")
        for retired in parse_keyring(os.environ.get("FIRM_AUDIT_RETIRED_KEYS")).values():
            add_key(self._keyring, retired, source="FIRM_AUDIT_RETIRED_KEYS")
        self.require_signatures = require_signatures
        self.store = ObjectStore(
            bucket, prefix=prefix, region=region, endpoint_url=endpoint_url, client=client
        )
        self.event_prefix = f"{self.store.prefix}events/v1/{self.stream}/"

    @property
    def key_id(self) -> str | None:
        """The ``key_id`` that signs new events, or ``None`` when writes are unsigned."""
        return self._key.id if self._key is not None else None

    @property
    def verifies_signatures(self) -> bool:
        """Whether this log holds any key to check signatures with (current or retired)."""
        return bool(self._keyring)

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
        body = canonical(envelope)
        if self._key is not None:
            # Signed once here, so every retry of this prepared event writes identical bytes.
            envelope["sig"] = {"key_id": self._key.id, "mac": s3_event_mac(self._key, body)}
            body = canonical(envelope)
        return PreparedEvent(
            f"{self.event_prefix}{timestamp.date().isoformat()}/{ident}.json", body.decode()
        )

    def _decode(self, key: str, body: bytes) -> dict[str, Any]:
        value = json.loads(body)
        if canonical(value) != body:
            raise ValueError("event body must retain the canonical format produced by prepare()")
        if value.get("v") != 1 or value.get("stream") != self.stream:
            raise ValueError("unsupported event format or stream")
        signature = self._check_signature(key, value)
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
            "signature": signature,
        }

    def _check_signature(self, key: str, envelope: dict[str, Any]) -> dict[str, str | None]:
        """Classify an envelope's signature: ``valid`` (verified with a configured key),
        ``unverified`` (signed by a key this log does not hold), or ``unsigned``. A signature
        that fails to verify, or is malformed, raises; so does anything but ``valid`` under
        ``require_signatures``. The stored value is attacker-writable, so its shape is checked
        before it reaches the MAC comparison."""
        sig = envelope.get("sig")
        signer: str | None = None
        if sig is None:
            state = "unsigned"
        else:
            if (
                not isinstance(sig, dict)
                or set(sig) != {"key_id", "mac"}
                or not isinstance(sig["key_id"], str)
                or not isinstance(sig["mac"], str)
                or len(sig["mac"]) != _MAC_HEX_LENGTH
            ):
                raise EventSignatureError(f"malformed event signature at {key}")
            signer = sig["key_id"]
            signing_key = self._keyring.get(signer)
            if signing_key is None:
                state = "unverified"
            else:
                unsigned = canonical({k: v for k, v in envelope.items() if k != "sig"})
                if not HmacSigner.tags_match(s3_event_mac(signing_key, unsigned), sig["mac"]):
                    raise EventSignatureError(f"event signature does not verify at {key}")
                state = "valid"
        if self.require_signatures and state != "valid":
            raise EventSignatureError(
                f"event at {key} is {state}; require_signatures accepts only events "
                "signed by a configured key"
            )
        return {"status": state, "key_id": signer}

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
