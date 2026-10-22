"""S3 objects behind the audit storage seam (:mod:`.storage`).

The S3 audit log keeps exactly what the SQL tables keep — event rows with the same columns, the
same activation/seal/floor records, the same verify-status row — as objects, so the very same
Sealer, Verifier and Retention code runs on it. Layout, per stream, under the store prefix::

    audit/v2/{stream}/seq.json                      id counter: one If-Match CAS per event
    audit/v2/{stream}/ev/{id:020d}-{entry}.json     one event row (the SQL columns verbatim)
    audit/v2/{stream}/ix/entry/{entry}              {"id", "at"}: a write's idempotency claim
    audit/v2/{stream}/ix/id/{inv(id)}-{entry}       every event, newest id first
    audit/v2/{stream}/ix/t/{inv(ts)}/{inv(id)}-{entry}   created_at, newest first (``since``)
    audit/v2/{stream}/ix/ta/{ts}/{id:020d}-{entry}  created_at, oldest first (retention)
    audit/v2/{stream}/ix/f/{field}/{hex}/{inv(id)}-{entry}  one per scalar filter value
    audit/v2/{stream}/ix/u/{inv(id)}-{ts}-{entry}   unsigned rows (the activation boundary)
    audit/v2/{stream}/seal/{coordinate}.json        activation / seal / floor records
    audit/v2/{stream}/status.json                   the verify-status row
    audit/v2/{stream}/lease.json                    the exclusive maintenance lease
    audit/v2/{stream}/prune.json                    a floor advance in progress

Event writes need no lease: each reserves an id with one compare-and-swap on ``seq.json`` and
claims it for its ``entry_id`` with a conditional create, so retries of one prepared event
converge on one id. Every maintenance scope (sealing, retention, verification) holds the lease,
which replaces the SQL snapshot/claim transactions; a floor advance that spans many objects is
written as an intent first and completed (idempotently) by whoever next holds the lease. Index
objects only serve ``history()`` and the dashboard; verification always reads ``ev/`` itself.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...._core.clock import now_utc
from ...event_format import canonical, uuid_text
from ...integrity import canonical_created_at
from ...object_store import ObjectConflict, ObjectStore, Receipt, digest, error_code
from ...prepared import (
    EVENT_FIELDS,
    PreparedEvent,
    UnknownEventOutcome,
    decode_prepared,
    freeze_values,
)
from .. import ACTIVATION_FROM_ID, SealConflict, StorageBusy

if TYPE_CHECKING:
    from ...verify import SealRecords

#: Signed scalar columns, in table order (``data``/``changes``/``context`` are stored JSON text).
ROW_FIELDS = EVENT_FIELDS

#: History filters with their own index, most selective first.
INDEXED_FILTERS = (
    "correlation_id",
    "subject",
    "actor",
    "subject_id",
    "actor_id",
    "action",
    "subject_type",
    "actor_type",
)
_ID_DIGITS = 20
_ID_SPAN = 10**_ID_DIGITS - 1
_SEQ_ATTEMPTS = 50


def inv(value: int) -> str:
    """Zero-padded inverted id: ascending keys list the highest id first."""
    return f"{_ID_SPAN - value:0{_ID_DIGITS}d}"


def ts_digits(created_at: str) -> str:
    """The canonical ``created_at`` as 20 sortable digits (``YYYYMMDDHHMMSSffffff``)."""
    return "".join(ch for ch in created_at if ch.isdigit())


def inv_digits(digits: str) -> str:
    return "".join(str(9 - int(ch)) for ch in digits)


def _hex(value: str) -> str:
    return value.encode("utf-8").hex()


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Layout:
    """Every object key of one stream."""

    root: str

    @property
    def seq(self) -> str:
        return f"{self.root}seq.json"

    @property
    def lease(self) -> str:
        return f"{self.root}lease.json"

    @property
    def prune(self) -> str:
        return f"{self.root}prune.json"

    @property
    def status(self) -> str:
        return f"{self.root}status.json"

    @property
    def events(self) -> str:
        return f"{self.root}ev/"

    @property
    def seals(self) -> str:
        return f"{self.root}seal/"

    def event(self, event_id: int, entry_id: str) -> str:
        return f"{self.root}ev/{event_id:0{_ID_DIGITS}d}-{entry_id}.json"

    def after_event(self, event_id: int) -> str:
        """A LIST ``StartAfter`` that skips every key of ``event_id`` and below."""
        return f"{self.root}ev/{event_id:0{_ID_DIGITS}d}."

    def parse_event(self, key: str) -> tuple[int, str] | None:
        name = key[len(self.events) :]
        if not name.endswith(".json") or "-" not in name:
            return None
        digits, _, entry = name[: -len(".json")].partition("-")
        if len(digits) != _ID_DIGITS or not digits.isdigit() or not entry:
            return None
        return int(digits), entry

    # The archive-before-prune ledger (the SQL archive_* tables). Membership lives entirely in
    # its key, so the prune guard reads keys only.
    @property
    def archive_policy(self) -> str:
        return f"{self.root}archive/policy.json"

    def archive_batch(self, batch_id: str) -> str:
        return f"{self.root}archive/batch/{batch_id}.json"

    @property
    def archive_pending(self) -> str:
        return f"{self.root}archive/pending/"

    @property
    def archive_acked(self) -> str:
        return f"{self.root}archive/acked/"

    @property
    def archive_members(self) -> str:
        return f"{self.root}archive/member/"

    def archive_member(self, event_id: int, fingerprint: str, batch_id: str) -> str:
        return f"{self.archive_members}{event_id:0{_ID_DIGITS}d}-{fingerprint}-{batch_id}"

    def parse_member(self, key: str) -> tuple[int, str, str] | None:
        name = key[len(self.archive_members) :]
        digits, fingerprint, batch_id = name[:_ID_DIGITS], name[21:85], name[86:]
        if not digits.isdigit() or len(fingerprint) != 64 or not batch_id:
            return None
        return int(digits), fingerprint, batch_id

    def entry(self, entry_id: str) -> str:
        return f"{self.root}ix/entry/{entry_id}"

    @property
    def id_index(self) -> str:
        return f"{self.root}ix/id/"

    @property
    def time_desc(self) -> str:
        return f"{self.root}ix/t/"

    @property
    def time_asc(self) -> str:
        return f"{self.root}ix/ta/"

    @property
    def unsigned(self) -> str:
        return f"{self.root}ix/u/"

    def field(self, name: str, value: str) -> str:
        return f"{self.root}ix/f/{name}/{_hex(value)}/"

    def seal(self, kind: str, from_id: int | None, to_id: int) -> str:
        if kind == "activation":
            return f"{self.root}seal/activation.json"
        if kind == "seal":
            # ``from_id`` is the arbiter, exactly like the SQL unique index on it.
            return f"{self.root}seal/s-{from_id:0{_ID_DIGITS}d}.json"
        return f"{self.root}seal/f-{to_id:0{_ID_DIGITS}d}.json"

    def markers(self, row: dict[str, Any]) -> list[str]:
        """Every index object for one event row."""
        event_id, entry_id = row["id"], row["entry_id"]
        stamp = ts_digits(row["created_at"])
        tail = f"{inv(event_id)}-{entry_id}"
        keys = [
            f"{self.id_index}{tail}",
            f"{self.time_desc}{inv_digits(stamp)}/{tail}",
            f"{self.time_asc}{stamp}/{event_id:0{_ID_DIGITS}d}-{entry_id}",
        ]
        for name, value in index_values(row).items():
            if value is not None:
                keys.append(f"{self.field(name, value)}{tail}")
        if row.get("row_mac") is None:
            keys.append(f"{self.unsigned}{inv(event_id)}-{stamp}-{entry_id}")
        return keys


def index_values(row: dict[str, Any]) -> dict[str, str | None]:
    """The value each history filter index holds for a row (``subject``/``actor`` pair both)."""

    def pair(kind: str | None, ident: str | None) -> str | None:
        return f"{kind}\x00{ident}" if kind is not None and ident is not None else None

    return {
        "correlation_id": row.get("correlation_id"),
        "subject": pair(row.get("subject_type"), row.get("subject_id")),
        "actor": pair(row.get("actor_type"), row.get("actor_id")),
        "subject_id": row.get("subject_id"),
        "actor_id": row.get("actor_id"),
        "action": row.get("action"),
        "subject_type": row.get("subject_type"),
        "actor_type": row.get("actor_type"),
    }


def parse_marker_tail(name: str) -> tuple[int, str] | None:
    """``{inv(id)}-{entry}`` (the last path segment of an index key) → ``(id, entry_id)``."""
    digits, _, entry = name.partition("-")
    if len(digits) != _ID_DIGITS or not digits.isdigit() or not entry:
        return None
    return _ID_SPAN - int(digits), entry


def encode_row(row: dict[str, Any]) -> bytes:
    """One event object: the SQL columns verbatim, canonical JSON."""
    body = {name: row.get(name) for name in ROW_FIELDS}
    body.update(
        v=2,
        id=row["id"],
        entry_id=row["entry_id"],
        created_at=row["created_at"],
        row_mac=row.get("row_mac"),
        key_id=row.get("key_id"),
    )
    if row.get("provenance") is not None:
        body["provenance"] = row["provenance"]
    return canonical(body)


def decode_row(body: bytes) -> dict[str, Any] | None:
    """A well-formed event object as a row dict, or ``None`` for anything malformed."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or value.get("v") != 2:
        return None
    if not isinstance(value.get("id"), int) or not isinstance(value.get("entry_id"), str):
        return None
    if not isinstance(value.get("created_at"), str) or _when(value["created_at"]) is None:
        return None
    for name in (*ROW_FIELDS, "row_mac", "key_id"):
        if value.get(name) is not None and not isinstance(value[name], str):
            return None
    if not isinstance(value.get("action"), str):
        return None
    return value


def projection(key_id: int, key_entry: str, body: bytes | None) -> SimpleNamespace:
    """The bounded row projection verification reads, matching the SQL projection:
    strings cut one past their column limit, ``created_at`` as text, and ``oversized`` set for
    anything a well-formed row could not hold — which classifies it as tampered."""
    from ...verify import _MAX_JSON_CHARS, _MAX_SCALAR_CHARS

    limits = dict.fromkeys(ROW_FIELDS, _MAX_SCALAR_CHARS)
    limits.update(data=_MAX_JSON_CHARS, changes=_MAX_JSON_CHARS, context=_MAX_JSON_CHARS)
    limits.update(entry_id=26, row_mac=64, key_id=16)
    row = decode_row(body) if body is not None else None
    values: dict[str, Any] = dict.fromkeys((*limits, "created_at"))
    oversized = True
    if row is not None and row["id"] == key_id and row["entry_id"] == key_entry:
        oversized = False
        for name, limit in limits.items():
            value = row.get(name)
            if value is not None and len(value) > limit:
                oversized = True
                value = value[: limit + 1]
            values[name] = value
        created = row["created_at"]
        if len(created) > 64:
            oversized = True
        values["created_at"] = created[:65]
    return SimpleNamespace(id=key_id, oversized=oversized, **values)


def row_dict(row: dict[str, Any]) -> dict[str, Any]:
    """An event as ``history()`` returns it: the SQL ``_row_to_dict`` shape."""
    from ...serialization import load_json

    created = datetime.fromisoformat(row["created_at"])
    return {
        "id": row["id"],
        "action": row["action"],
        "subject_type": row.get("subject_type"),
        "subject_id": row.get("subject_id"),
        "subject_label": row.get("subject_label"),
        "actor_type": row.get("actor_type"),
        "actor_id": row.get("actor_id"),
        "actor_label": row.get("actor_label"),
        "correlation_id": row.get("correlation_id"),
        "data": load_json(row.get("data")),
        "changes": load_json(row.get("changes")),
        "context": load_json(row.get("context")),
        "created_at": created,
    }


class Lease:
    """The exclusive maintenance lease: one small object, taken and renewed by CAS."""

    def __init__(self, store: ObjectStore, key: str, *, ttl: float) -> None:
        self.store = store
        self.key = key
        self.ttl = ttl
        self.holder = uuid4().hex
        self._renewed = 0.0

    def _body(self, expires: datetime) -> bytes:
        return canonical({"holder": self.holder, "expires": expires.isoformat()})

    def acquire(self, wait: float) -> None:
        deadline = time.monotonic() + wait
        while True:
            current = self.store.read_tagged(self.key)
            now = now_utc()
            if current is None:
                taken = self.store.swap(self.key, self._body(now + self._span), etag=None)
            else:
                data, etag = current
                try:
                    state = json.loads(data)
                    expires = datetime.fromisoformat(state["expires"])
                    holder = state["holder"]
                except (ValueError, KeyError, TypeError):
                    expires, holder = now, None  # a malformed lease is free to take over
                if holder != self.holder and expires > now:
                    taken = False
                else:
                    taken = self.store.swap(self.key, self._body(now + self._span), etag=etag)
            if taken:
                self._renewed = time.monotonic()
                return
            if time.monotonic() >= deadline:
                raise StorageBusy("another process holds the S3 audit maintenance lease")
            time.sleep(min(1.0, max(0.05, wait / 20)))

    @property
    def _span(self) -> timedelta:
        return timedelta(seconds=self.ttl)

    def renew(self) -> None:
        """Extend the lease when a third of it has passed; fail if someone else took it."""
        if time.monotonic() - self._renewed < self.ttl / 3:
            return
        current = self.store.read_tagged(self.key)
        if current is None or json.loads(current[0]).get("holder") != self.holder:
            raise StorageBusy("the S3 audit maintenance lease was lost")
        if not self.store.swap(self.key, self._body(now_utc() + self._span), etag=current[1]):
            raise StorageBusy("the S3 audit maintenance lease was lost")
        self._renewed = time.monotonic()

    def release(self) -> None:
        current = self.store.read_tagged(self.key)
        if current is None:
            return
        try:
            mine = json.loads(current[0]).get("holder") == self.holder
        except ValueError:
            mine = False
        if mine:
            self.store.swap(self.key, self._body(now_utc() - timedelta(seconds=1)), etag=current[1])


class S3View:
    """One maintenance scope (or an unlocked read) over the stream's objects."""

    sql = False

    def __init__(self, storage: S3Storage, lease: Lease | None) -> None:
        self.storage = storage
        self.store = storage.store
        self.layout = storage.layout
        self.lease = lease
        self._expired_rows: dict[int, dict[str, Any]] = {}

    def _renew(self) -> None:
        if self.lease is not None:
            self.lease.renew()

    # -- seal records -----------------------------------------------------------------------------

    def _seal_rows(self) -> list[dict[str, Any] | None]:
        keys = list(self.store.iter_keys(self.layout.seals))
        rows: list[dict[str, Any] | None] = []
        for _, body in self.storage.fetch(keys):
            try:
                value = json.loads(body) if body is not None else None
            except (ValueError, UnicodeDecodeError):
                value = None
            rows.append(value if isinstance(value, dict) else None)
        return rows

    def seal_records(self) -> SealRecords:
        from ...verify import _MAX_SEAL_RECORDS, SealRecords, _parse_seal_record

        def text(value: Any, chars: int) -> str | None:
            return None if value is None else str(value)[: chars + 1]

        records = SealRecords()
        parsed = []
        synthetic = -1
        for row in self._seal_rows():
            if row is None:
                row = {}
            record_id = row.get("id")
            if not isinstance(record_id, int) or isinstance(record_id, bool):
                # A malformed object still has to surface (and fail) in verification.
                record_id, synthetic = synthetic, synthetic - 1
            parsed.append(
                _parse_seal_record(
                    SimpleNamespace(
                        id=record_id,
                        kind=text(row.get("kind"), 32),
                        from_id=text(row.get("from_id"), 64),
                        to_id=text(row.get("to_id"), 64),
                        row_count=text(row.get("row_count"), 64),
                        rows_mac=text(row.get("rows_mac"), 64),
                        seal_mac=text(row.get("seal_mac"), 64),
                        sealed_at=text(row.get("sealed_at"), 64),
                        key_id=text(row.get("key_id"), 16),
                    )
                )
            )
        parsed.sort(key=lambda record: record.id)
        records.extend(parsed[:_MAX_SEAL_RECORDS])
        records.capped = len(parsed) > _MAX_SEAL_RECORDS
        return records

    def any_seal_record(self) -> bool:
        return bool(self.store.list_keys(self.layout.seals, limit=1)[0])

    def activation_exists(self) -> bool:
        return self.store.exists(self.layout.seal("activation", ACTIVATION_FROM_ID, 0))

    def lock_activation(self) -> bool:
        # The lease already makes this scope exclusive; only presence matters here.
        return self.activation_exists()

    def signer_mismatch(self, allowed: Sequence[str]) -> tuple[bool, str | None]:
        permitted = set(allowed)
        for record in self.seal_records():
            if record.key_id is None or record.key_id not in permitted:
                return True, record.key_id
        return False, None

    def seal_hwm(self) -> int:
        return max(
            (
                record.to_id
                for record in self.seal_records()
                if record.kind in {"activation", "floor", "seal"} and record.to_id is not None
            ),
            default=0,
        )

    def insert_seal(self, **values: Any) -> None:
        records = self.seal_records()
        record = {
            "id": max((r.id for r in records), default=0) + 1,
            **values,
            "sealed_at": canonical_created_at(values["sealed_at"]),
        }
        key = self.layout.seal(values["kind"], values["from_id"], values["to_id"])
        if not self.store.swap(key, canonical(record), etag=None):
            raise SealConflict(f"a seal record already exists at {key}")

    # -- rows -------------------------------------------------------------------------------------

    def _event_keys(self, after: int = 0) -> Iterator[tuple[int, str, str]]:
        """``(id, entry_id, key)`` for every event above ``after``, in id order."""
        start = self.layout.after_event(after) if after > 0 else ""
        for key in self.store.iter_keys(self.layout.events, after=start):
            parsed = self.layout.parse_event(key)
            if parsed is None:
                # A stray object under ev/ cannot be a row; surface it as one that fails.
                yield -1, "", key
                continue
            yield parsed[0], parsed[1], key

    def iter_rows(self, low: int, high: int | None) -> Iterator[Any]:
        page: list[tuple[int, str, str]] = []
        for event_id, entry_id, key in self._event_keys(max(low, 0)):
            if event_id < 0:
                continue
            if event_id <= low:
                continue
            if high is not None and event_id > high:
                break
            page.append((event_id, entry_id, key))
            if len(page) == 1000:
                yield from self._project(page)
                page = []
        if page:
            yield from self._project(page)

    def _project(self, page: list[tuple[int, str, str]]) -> Iterator[SimpleNamespace]:
        self._renew()
        bodies = dict(self.storage.fetch([key for _, _, key in page]))
        for event_id, entry_id, key in page:
            yield projection(event_id, entry_id, bodies.get(key))

    def any_row(self) -> bool:
        return bool(self.store.list_keys(self.layout.events, limit=1)[0])

    def any_row_between(self, low: int, high: int) -> bool:
        for event_id, _, _ in self._event_keys(max(low, 0)):
            if event_id < 0:
                continue
            return low < event_id <= high
        return False

    def rows_at_or_below(self, floor: int, limit: int) -> list[int]:
        found = []
        for event_id, _, _ in self._event_keys():
            if event_id < 0:
                continue
            if event_id > floor or len(found) >= limit:
                break
            found.append(event_id)
        return found

    def duplicate_entry_ids(self, limit: int) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for event_id, entry_id, _ in self._event_keys():
            if event_id < 0:
                continue
            if entry_id in seen and entry_id not in duplicates:
                duplicates.append(entry_id)
                if len(duplicates) >= limit:
                    break
            seen.add(entry_id)
        return duplicates

    def _existing_ids(self, ids: Iterable[int]) -> set[int]:
        wanted = sorted(set(ids))
        if not wanted:
            return set()
        present = set()
        for event_id, _, _ in self._event_keys(wanted[0] - 1):
            if event_id > wanted[-1]:
                break
            if event_id in wanted:
                present.add(event_id)
        return present

    def max_unsigned_settled_id(self, cutoff: datetime) -> int:
        limit = ts_digits(canonical_created_at(cutoff))
        for key in self.store.iter_keys(self.layout.unsigned):
            parts = key[len(self.layout.unsigned) :].split("-", 2)
            if len(parts) != 3:
                continue
            event_id = _ID_SPAN - int(parts[0]) if parts[0].isdigit() else -1
            if parts[1] <= limit and event_id > 0 and self._existing_ids([event_id]):
                return event_id
        return 0

    def settled_rows_after(self, hwm: int, cutoff: datetime, limit: int) -> list[Any]:
        rows: list[Any] = []
        page: list[tuple[int, str, str]] = []

        def drain() -> bool:
            """Append the page's settled rows; ``True`` once the settled prefix has ended."""
            self._renew()
            bodies = dict(self.storage.fetch([key for _, _, key in page]))
            for event_id, _, key in page:
                body = bodies.get(key)
                row = decode_row(body) if body is not None else None
                created = _when(row["created_at"]) if row is not None else None
                if created is not None and created > cutoff:
                    return True
                # A malformed object reads as unsigned, so the sealer refuses it loudly.
                mac = row.get("row_mac") if row is not None else None
                rows.append(SimpleNamespace(id=event_id, row_mac=mac))
                if len(rows) >= limit:
                    return True
            return False

        for event_id, entry_id, key in self._event_keys(max(hwm, 0)):
            if event_id <= hwm:
                continue
            page.append((event_id, entry_id, key))
            if len(page) == 1000:
                if drain():
                    return rows
                page = []
        if page:
            drain()
        return rows

    # -- time-based reads --------------------------------------------------------------------------

    def _expired_ids(self, before: datetime) -> Iterator[tuple[int, str]]:
        """``(id, entry_id)`` of index entries created before ``before``, oldest first."""
        limit = ts_digits(canonical_created_at(before))
        for key in self.store.iter_keys(self.layout.time_asc):
            stamp, _, tail = key[len(self.layout.time_asc) :].partition("/")
            if stamp >= limit:
                return
            digits, _, entry = tail.partition("-")
            if digits.isdigit() and entry:
                yield int(digits), entry

    def has_expired(self, cutoff: datetime) -> bool:
        return any(self._existing_ids([event_id]) for event_id, _ in self._expired_ids(cutoff))

    def count_rows(self, *, after: int, before: datetime, through: int | None = None) -> int:
        ids = [
            event_id
            for event_id, _ in self._expired_ids(before)
            if event_id > after and (through is None or event_id <= through)
        ]
        return len(self._existing_ids(ids))

    def rows_between(self, low: int, high: int) -> list[Any]:
        """Full rows with id in ``(low, high]`` (the archive guard's candidates)."""
        keys = []
        for event_id, _, key in self._event_keys(max(low, 0)):
            if event_id <= low:
                continue
            if event_id > high:
                break
            keys.append(key)
        rows = []
        for _, body in self.storage.fetch(keys):
            row = decode_row(body) if body is not None else None
            if row is not None:
                rows.append(SimpleNamespace(**row))
        return rows

    def expired_batch(self, cutoff: datetime, offset: int, limit: int) -> list[Any]:
        candidates = []
        for event_id, entry_id in self._expired_ids(cutoff):
            candidates.append((event_id, entry_id))
            if len(candidates) >= offset + limit:
                break
        keys = [self.layout.event(event_id, entry_id) for event_id, entry_id in candidates]
        found = []
        for _, body in self.storage.fetch(keys):
            row = decode_row(body) if body is not None else None
            if row is not None:
                self._expired_rows[row["id"]] = row
                found.append(SimpleNamespace(**row))
        found.sort(key=lambda row: row.id)
        return found[offset : offset + limit]

    # -- archive-before-prune ----------------------------------------------------------------------

    def archive_policy(self, *, schema_present: bool | None = None) -> Any:
        """The persisted archive policy (stream/destination unset until ``enable()``)."""
        current = self.store.read_tagged(self.layout.archive_policy)
        value = json.loads(current[0]) if current is not None else {}
        return SimpleNamespace(stream=value.get("stream"), destination=value.get("destination"))

    def set_archive_policy(self, stream: str, destination: str) -> None:
        current = self.store.read_tagged(self.layout.archive_policy)
        body = canonical({"stream": stream, "destination": destination})
        if not self.store.swap(
            self.layout.archive_policy, body, etag=current[1] if current else None
        ):
            raise StorageBusy("the archive policy changed while enabling it")

    def pending_archive_batch(self) -> tuple[str, str | None] | None:
        for key in self.store.iter_keys(self.layout.archive_pending):
            batch_id = key[len(self.layout.archive_pending) :]
            current = self.store.read_tagged(self.layout.archive_batch(batch_id))
            body = json.loads(current[0]).get("body") if current is not None else None
            return batch_id, body
        return None

    def _members(self, low: int = 0, high: int | None = None) -> dict[int, tuple[str, str]]:
        """``event_id → (fingerprint, batch_id)`` for members with id in ``(low, high]``."""
        start = f"{self.layout.archive_members}{low:0{_ID_DIGITS}d}." if low > 0 else ""
        found: dict[int, tuple[str, str]] = {}
        for key in self.store.iter_keys(self.layout.archive_members, after=start):
            parsed = self.layout.parse_member(key)
            if parsed is None:
                continue
            event_id, fingerprint, batch_id = parsed
            if high is not None and event_id > high:
                break
            found[event_id] = (fingerprint, batch_id)
        return found

    def unreserved_rows(self, limit: int) -> list[Any]:
        reserved = self._members()
        wanted: list[str] = []
        for event_id, _, key in self._event_keys():
            if event_id > 0 and event_id not in reserved:
                wanted.append(key)
                if len(wanted) >= limit:
                    break
        rows = []
        for _, body in self.storage.fetch(wanted):
            row = decode_row(body) if body is not None else None
            if row is not None:
                rows.append(row)
        return rows

    def archive_entry(self, stream: str, row: Any) -> dict[str, Any]:
        from ...archive import row_entry

        return row_entry("s3", stream, row if isinstance(row, dict) else vars(row))

    def reserve_archive_batch(
        self, batch_id: str, body: str, members: Sequence[dict[str, Any]]
    ) -> None:
        batch = canonical({"id": batch_id, "body": body, "receipt": None})
        if not self.store.swap(self.layout.archive_batch(batch_id), batch, etag=None):
            raise StorageBusy("an archive batch with this id already exists")
        self.storage._create_all(
            [
                f"{self.layout.archive_pending}{batch_id}",
                *(
                    self.layout.archive_member(m["event_id"], m["fingerprint"], m["batch_id"])
                    for m in members
                ),
            ]
        )

    def verified_ids(
        self, stream: str, rows: Sequence[Any], *, span: tuple[int, int] | None = None
    ) -> set[int]:
        """Ids among ``rows`` whose exact content sits in an acknowledged batch (keys only,
        plus recomputing each row's fingerprint, as the SQL guard does)."""
        from ...archive import row_entry
        from ...archive.store import fingerprint

        if not rows:
            return set()
        by_id = {row.id: (row if isinstance(row, dict) else vars(row)) for row in rows}
        low, high = span if span is not None else (min(by_id) - 1, max(by_id))
        members = self._members(max(low, 0), high)
        acked = {
            key[len(self.layout.archive_acked) :]
            for key in self.store.iter_keys(self.layout.archive_acked)
        }
        return {
            event_id
            for event_id, row in by_id.items()
            if event_id in members
            and members[event_id][1] in acked
            and fingerprint(row_entry("s3", stream, row)) == members[event_id][0]
        }

    # -- deletion ----------------------------------------------------------------------------------

    def delete_rows(self, ids: Sequence[int], *, archive_tables: bool) -> None:
        rows = [self._expired_rows[event_id] for event_id in ids if event_id in self._expired_rows]
        self.storage.delete_events(rows)
        if archive_tables and ids:
            gone = set(ids)
            members = self._members(min(gone) - 1, max(gone))
            self.store.delete_keys(
                self.layout.archive_member(event_id, fingerprint, batch_id)
                for event_id, (fingerprint, batch_id) in members.items()
                if event_id in gone
            )

    def advance_floor(self, floor: dict[str, Any], *, through: int, archive_tables: bool) -> int:
        """Record the floor advance as an intent, then complete it. Many objects change, so
        the intent is what makes this atomic in effect: any later lease holder finishes it."""
        intent = canonical({"through": through, "floor": _jsonable(floor)})
        while not self.store.swap(self.layout.prune, intent, etag=None):
            self.complete_pending_prune()  # never expected under the lease; finish it first
        return self.complete_pending_prune()

    def complete_pending_prune(self) -> int:
        """Finish a floor advance whose intent is on record (idempotent; resumes after a crash):
        floor record, then every event and index object at or below it, then the seals it
        covered, then the intent itself."""
        current = self.store.read_tagged(self.layout.prune)
        if current is None:
            return 0
        intent = json.loads(current[0])
        through = int(intent["through"])
        floor = dict(intent["floor"])
        floor["sealed_at"] = datetime.fromisoformat(floor["sealed_at"])
        with suppress(SealConflict):  # already written by an interrupted attempt
            self.insert_seal(**floor)
        # Covered seals go before their rows: a reader without the lease that looks mid-way then
        # sees an honest unsealed tail, never seals over rows that are half gone.
        covered = [
            self.layout.seal("seal", record.from_id, record.to_id or 0)
            for record in self.seal_records()
            if record.kind == "seal" and record.to_id is not None and record.to_id <= through
        ]
        self.store.delete_keys(covered)
        deleted = 0
        page: list[str] = []
        for event_id, _, key in self._event_keys():
            if event_id > through:
                break
            page.append(key)
            if len(page) == 1000:
                deleted += self._delete_event_keys(page)
                page = []
        if page:
            deleted += self._delete_event_keys(page)
        self.store.delete_keys(
            self.layout.archive_member(event_id, fingerprint, batch_id)
            for event_id, (fingerprint, batch_id) in self._members(0, through).items()
        )
        self.store.delete_keys([self.layout.prune])
        return deleted

    def _delete_event_keys(self, keys: list[str]) -> int:
        self._renew()
        rows = []
        stray = []
        for key, body in self.storage.fetch(keys):
            row = decode_row(body) if body is not None else None
            if row is None:
                stray.append(key)
            else:
                rows.append(row)
        self.storage.delete_events(rows)
        self.store.delete_keys(stray)
        return len(rows) + len(stray)

    # -- verify status -----------------------------------------------------------------------------

    def status_prior(self) -> Any:
        status = self.storage.status()
        if status is None:
            return None
        return SimpleNamespace(
            last_full_coverage_at=_when(status.get("last_full_coverage_at")),
            sealing_observed=bool(status.get("sealing_observed")),
        )


def _naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _jsonable(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value.isoformat() if isinstance(value, datetime) else value
        for name, value in values.items()
    }


class S3Storage:
    """One stream's objects behind the storage seam, plus the event write path."""

    sql = False
    requires_entry_id = True

    def __init__(
        self,
        bucket: ObjectStore | str,
        stream: str,
        *,
        prefix: str = "firm-audit",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
        workers: int = 16,
        lease_ttl: float = 300.0,
        lease_wait: float = 5.0,
    ) -> None:
        self._closed = False
        self.stream = uuid_text(stream)
        self.store = (
            bucket
            if isinstance(bucket, ObjectStore)
            else ObjectStore(
                bucket,
                prefix=prefix,
                region=region,
                endpoint_url=endpoint_url,
                client=client,
            )
        )
        self.layout = Layout(f"{self.store.prefix}audit/v2/{self.stream}/")
        self.lease_ttl = lease_ttl
        self.lease_wait = lease_wait
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="firm-audit-s3")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=True)
        self.store.close()

    # -- concurrency scopes ------------------------------------------------------------------------

    @contextmanager
    def _maintenance(self, wait: float | None = None) -> Iterator[S3View]:
        lease = Lease(self.store, self.layout.lease, ttl=self.lease_ttl)
        lease.acquire(self.lease_wait if wait is None else wait)
        try:
            view = S3View(self, lease)
            view.complete_pending_prune()
            yield view
        finally:
            lease.release()

    @contextmanager
    def snapshot(self, *, write: bool = False) -> Iterator[S3View]:
        # Verification (the read snapshot) waits out a whole lease rather than fail: sealing and
        # retention skip a busy round instead (see Sealer/Retention), so verify gets its turn.
        with self._maintenance(None if write else self.lease_ttl) as view:
            yield view

    @contextmanager
    def claim(self) -> Iterator[S3View]:
        with self._maintenance() as view:
            yield view

    @contextmanager
    def connect(self) -> Iterator[S3View]:
        yield S3View(self, None)

    # -- object helpers ----------------------------------------------------------------------------

    def fetch(self, keys: Sequence[str]) -> list[tuple[str, bytes | None]]:
        """Read many objects concurrently, in order; a missing object reads as ``None``."""

        def one(key: str) -> tuple[str, bytes | None]:
            try:
                return key, self.store.read(key)[0]
            except FileNotFoundError:
                return key, None

        return list(self._pool.map(one, keys))

    def _create_all(self, keys: Sequence[str], body: bytes = b"") -> None:
        def one(key: str) -> None:
            self.store.swap(key, body, etag=None)  # False: it is already there

        list(self._pool.map(one, keys))

    def delete_events(self, rows: Sequence[dict[str, Any]]) -> None:
        keys: list[str] = []
        for row in rows:
            keys.append(self.layout.event(row["id"], row["entry_id"]))
            keys.append(self.layout.entry(row["entry_id"]))
            keys.extend(self.layout.markers(row))
        self.store.delete_keys(keys)

    # -- verify status -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any] | None:
        current = self.store.read_tagged(self.layout.status)
        if current is None:
            return None
        try:
            value = json.loads(current[0])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def acknowledge_archive_batch(self, batch_id: str, receipt: str) -> None:
        key = self.layout.archive_batch(batch_id)
        current = self.store.read_tagged(key)
        body = canonical({"id": batch_id, "body": None, "receipt": receipt})
        self.store.swap(key, body, etag=current[1] if current else None)
        self._create_all([f"{self.layout.archive_acked}{batch_id}"])
        self.store.delete_keys([f"{self.layout.archive_pending}{batch_id}"])

    def archive_status(self) -> dict[str, int]:
        view = S3View(self, None)
        present = {event_id for event_id, _, _ in view._event_keys() if event_id > 0}
        acked = {
            key[len(self.layout.archive_acked) :]
            for key in self.store.iter_keys(self.layout.archive_acked)
        }
        archived = sum(
            1
            for event_id, (_, batch_id) in view._members().items()
            if event_id in present and batch_id in acked
        )
        pending = sum(1 for _ in self.store.iter_keys(self.layout.archive_pending))
        return {
            "events": len(present),
            "unarchived": len(present) - archived,
            "pending_batches": pending,
        }

    def prior_sealing_observed(self) -> bool:
        status = self.status()
        return bool(status and status.get("sealing_observed"))

    def upsert_status(self, values: dict[str, Any]) -> None:
        for _ in range(_SEQ_ATTEMPTS):
            current = self.store.read_tagged(self.layout.status)
            base: dict[str, Any] = {}
            if current is not None:
                try:
                    loaded = json.loads(current[0])
                    base = loaded if isinstance(loaded, dict) else {}
                except ValueError:
                    base = {}
            merged = {"id": 1, **base, **_jsonable(values)}
            if self.store.swap(
                self.layout.status,
                canonical(merged),
                etag=current[1] if current else None,
            ):
                return
        raise StorageBusy("could not update the S3 audit verify status")

    # -- event writes ------------------------------------------------------------------------------

    def reserve_id(self) -> int:
        """Take the next id with one compare-and-swap on the stream counter."""
        for attempt in range(_SEQ_ATTEMPTS):
            current = self.store.read_tagged(self.layout.seq)
            if current is None:
                event_id, etag = 1, None
            else:
                event_id, etag = int(json.loads(current[0])["next"]), current[1]
            if self.store.swap(self.layout.seq, canonical({"next": event_id + 1}), etag=etag):
                return event_id
            time.sleep(random.uniform(0, 0.01 * (attempt + 1)))  # jitter only
        raise StorageBusy("the S3 audit id counter is too contended; retry the write")

    def publish(self, fields: dict[str, Any], *, grace: float) -> dict[str, Any]:
        """Write one prepared row (no id yet) and return it with its id.

        The ``entry_id`` claim makes this idempotent: a retry of the same prepared event finds
        its id and finishes or confirms the write. A claim whose event never appeared and is
        older than ``grace`` belonged to a writer that died; like a rolled-back SQL insert, the
        retry then takes a fresh id rather than land inside a range that may since be sealed.
        """
        entry_id = fields["entry_id"]
        claim_key = self.layout.entry(entry_id)
        for _ in range(_SEQ_ATTEMPTS):
            claim = self.store.read_tagged(claim_key)
            if claim is not None:
                state = json.loads(claim[0])
                event_id = int(state["id"])
                row = {**fields, "id": event_id}
                event_key = self.layout.event(event_id, entry_id)
                try:
                    stored, _ = self.store.read(event_key)
                except FileNotFoundError:
                    pass
                else:
                    if stored != encode_row(row):
                        raise ObjectConflict(
                            "prepared event identity already contains different bytes"
                        )
                    return row
                claimed_at = _when(state.get("at"))
                fresh = claimed_at is not None and now_utc() - claimed_at <= timedelta(
                    seconds=grace
                )
                if fresh:
                    self._write_row(row)
                    return row
                event_id = self.reserve_id()
                if not self.store.swap(claim_key, self._claim(event_id), etag=claim[1]):
                    continue  # another retrier re-claimed it first; use theirs
            else:
                event_id = self.reserve_id()
                if not self.store.swap(claim_key, self._claim(event_id), etag=None):
                    continue  # a concurrent write of this event claimed an id; use that one
            row = {**fields, "id": event_id}
            self._write_row(row)
            return row
        raise StorageBusy("could not claim an id for this audit event; retry the write")

    @staticmethod
    def _claim(event_id: int) -> bytes:
        return canonical({"id": event_id, "at": now_utc().isoformat()})

    def _write_row(self, row: dict[str, Any]) -> None:
        # Index objects first: once the event object exists, every index already points at it.
        self._create_all(self.layout.markers(row))
        self.store.put(self.layout.event(row["id"], row["entry_id"]), encode_row(row))

    # -- export / restore -----------------------------------------------------------------------

    def rows_after(
        self,
        after: int,
        limit: int,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` well-formed rows with id > ``after`` (and ``since <= created_at <
        until``), in id order — the S3 side of ``export_sql``'s query."""
        low, high = _naive(since), _naive(until)
        found: list[dict[str, Any]] = []
        page: list[str] = []

        def drain() -> None:
            for _, body in self.fetch(page):
                row = decode_row(body) if body is not None else None
                if row is None:
                    continue
                created = datetime.fromisoformat(row["created_at"])
                if (low is None or created >= low) and (high is None or created < high):
                    found.append(row)

        for event_id, _, key in S3View(self, None)._event_keys(after):
            if event_id <= after:
                continue
            page.append(key)
            if len(page) == 200:
                drain()
                page = []
                if len(found) >= limit:
                    break
        if page and len(found) < limit:
            drain()
        return found[:limit]

    def count_events(self) -> int:
        """How many event objects the stream holds (keys only: one LIST per 1,000)."""
        return sum(1 for event_id, _, _ in S3View(self, None)._event_keys() if event_id > 0)

    def is_empty(self) -> bool:
        """No events, integrity records, or verify status: an isolated restore target."""
        view = S3View(self, None)
        return not view.any_row() and not view.any_seal_record() and self.status() is None

    def restore_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        """Write archived rows back under their original ids, then move the counter past them."""
        for row in rows:
            self.store.swap(self.layout.entry(row["entry_id"]), self._claim(row["id"]), etag=None)
            self._write_row(row)
        if not rows:
            return
        top = max(row["id"] for row in rows)
        for _ in range(_SEQ_ATTEMPTS):
            current = self.store.read_tagged(self.layout.seq)
            if current is not None and int(json.loads(current[0])["next"]) > top:
                return
            if self.store.swap(
                self.layout.seq,
                canonical({"next": top + 1}),
                etag=current[1] if current else None,
            ):
                return
        raise StorageBusy("could not advance the S3 audit id counter after restore")

    # -- reads for history -------------------------------------------------------------------------

    def index_entries(self, prefix: str) -> Iterator[tuple[int, str]]:
        """``(id, entry_id)`` from an inverted-id index, highest id first."""
        for key in self.store.iter_keys(prefix):
            parsed = parse_marker_tail(key.rsplit("/", 1)[-1])
            if parsed is not None:
                yield parsed

    def time_entries(self, since: datetime | None) -> Iterator[tuple[int, str]]:
        """``(id, entry_id)`` created at or after ``since``, newest ``created_at`` first."""
        floor = ts_digits(canonical_created_at(since)) if since is not None else None
        for key in self.store.iter_keys(self.layout.time_desc):
            stamp, _, tail = key[len(self.layout.time_desc) :].partition("/")
            if floor is not None and inv_digits(stamp) < floor:
                return
            parsed = parse_marker_tail(tail)
            if parsed is not None:
                yield parsed

    def rows(self, entries: Sequence[tuple[int, str]]) -> list[dict[str, Any]]:
        """The rows behind index entries, in the given order; dangling entries are skipped."""
        keys = [self.layout.event(event_id, entry_id) for event_id, entry_id in entries]
        found = []
        for (event_id, entry_id), (_, body) in zip(entries, self.fetch(keys), strict=True):
            row = decode_row(body) if body is not None else None
            if row is not None and row["id"] == event_id and row["entry_id"] == entry_id:
                found.append(row)
        return found

    def read_row(self, event_id: int) -> dict[str, Any] | None:
        for found_id, entry_id, key in S3View(self, None)._event_keys(event_id - 1):
            if found_id != event_id:
                return None
            body = self.fetch([key])[0][1]
            row = decode_row(body) if body is not None else None
            return row if row is not None and row["entry_id"] == entry_id else None
        return None

    def record(self, values: dict[str, Any], *, conn: Any = None, grace: float) -> Receipt | None:
        """Persist an already signed event, or stage it in the caller's SQL transaction."""
        prepared = freeze_values(values)
        if conn is not None:
            from .outbox import stage

            stage(conn, self.stream, prepared.body)
            return None
        return self.write(prepared, grace=grace)

    def write(self, prepared: PreparedEvent, *, grace: float) -> Receipt:
        """Publish exact prepared bytes; preserve the retry token on ambiguous failures."""
        fields = decode_prepared(prepared)
        try:
            row = self.publish(fields, grace=grace)
        except ValueError:
            raise
        except Exception as exc:
            if error_code(exc) in {
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "NoSuchBucket",
            }:
                raise
            raise UnknownEventOutcome(prepared) from exc
        return Receipt(self.layout.event(row["id"], row["entry_id"]), digest(encode_row(row)))

    def get(self, locator: int | Receipt | str) -> dict[str, Any] | None:
        if isinstance(locator, int):
            row = self.read_row(locator)
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
        row = self.read_row(event_id)
        return row.get("provenance") if row is not None else None

    def history(
        self, wanted: dict[str, str], since: datetime | None, limit: int
    ) -> list[dict[str, Any]]:
        if since is not None and since.tzinfo is not None:
            since = since.astimezone(UTC).replace(tzinfo=None)
        return [row_dict(row) for row in self.matching(wanted, since, limit)]

    def matching(
        self, wanted: dict[str, str], since: datetime | None, limit: int
    ) -> list[dict[str, Any]]:
        """Raw rows matching exact column values (and ``since``), newest id first."""
        probe = {**wanted}
        if "subject_type" in wanted and "subject_id" in wanted:
            probe["subject"] = f"{wanted['subject_type']}\x00{wanted['subject_id']}"
        if "actor_type" in wanted and "actor_id" in wanted:
            probe["actor"] = f"{wanted['actor_type']}\x00{wanted['actor_id']}"
        layout = self.layout
        index = next((name for name in _INDEX_ORDER if name in probe), None)
        if index is not None:
            entries = self.index_entries(layout.field(index, probe[index]))
        elif since is not None:
            # Every event since ``since``, ordered newest id first like the SQL query.
            entries = iter(sorted(self.time_entries(since), reverse=True))
        else:
            entries = self.index_entries(layout.id_index)
        found: list[dict[str, Any]] = []
        page: list[tuple[int, str]] = []

        def drain() -> bool:
            for row in self.rows(page):
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


_HISTORY_PAGE = 100

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


def _matches(row: dict[str, Any], wanted: dict[str, str], since: datetime | None) -> bool:
    if any(row.get(name) != value for name, value in wanted.items()):
        return False
    return since is None or datetime.fromisoformat(row["created_at"]) >= since
