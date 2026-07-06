"""The ``Cache`` — a database-backed key/value store."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import SQLAlchemyError

from .._core.clock import now_utc
from .._core.database import (
    create_engine_for,
    dispose_engine,
    immediate_transaction,
    transaction,
)
from .._core.poller import default_on_error
from . import schema
from .entries import delete_entry, ensure_entry, read_entry, read_entry_locked, write_entry
from .expiry import Expiry, ExpiryLoop
from .keys import key_hash, normalize_key
from .serialization import Coder, JSONCoder, build_encrypted_coder

_entries = schema.entries

# Sentinel for "row exists but cannot be decoded" (rotated encryption key, coder change,
# corrupt bytes). Reads treat it as a miss instead of poisoning every access to the key.
_UNDECODABLE = object()

_T = TypeVar("_T")

TWO_WEEKS_SECONDS = 14 * 24 * 3600.0
DEFAULT_MAX_SIZE = 256 * 1024 * 1024


class Cache:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        coder: Coder | None = None,
        encrypt_key: str | bytes | Sequence[str | bytes] | None = None,
        max_age: float | None = TWO_WEEKS_SECONDS,
        max_size: int | None = DEFAULT_MAX_SIZE,
        max_entries: int | None = None,
        expiry_batch_size: int = 100,
        max_key_bytesize: int = 1024,
        size_estimate_samples: int = 10000,
        create_schema: bool = True,
        auto_expire: bool = True,
        background_expiry: bool = False,
        expiry_interval: float = 60.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self._owns_engine = False
        elif database_url is not None:
            self.engine = create_engine_for(database_url)
            self._owns_engine = True
        else:
            raise ValueError("Cache requires either database_url or engine")

        # JSON by default: decoding it is safe no matter who wrote the row. PickleCoder
        # (arbitrary objects, executes code on load) is a deliberate opt-in.
        base: Coder = coder or JSONCoder()
        self.encrypted = encrypt_key is not None
        self.coder: Coder = (
            build_encrypted_coder(base, encrypt_key) if encrypt_key is not None else base
        )

        self.max_age = max_age
        self.max_size = max_size
        self.max_entries = max_entries
        self.auto_expire = auto_expire
        self.expiry_batch_size = expiry_batch_size
        self.max_key_bytesize = max_key_bytesize
        self.size_estimate_samples = size_estimate_samples

        if create_schema:
            schema.create_all(self.engine)

        # Background eviction failures are routed here (default: traceback to stderr) — a
        # cache that silently stops evicting is a full-disk incident waiting to happen.
        self.on_error = on_error if on_error is not None else default_on_error
        self.expiry = Expiry(self)
        self._loop = ExpiryLoop(self.expiry, expiry_interval) if background_expiry else None
        if self._loop is not None:
            self._loop.start()

    def _kb(self, key: str | bytes) -> bytes:
        return normalize_key(key, self.max_key_bytesize)

    def _min_created_at(self) -> datetime | None:
        """Reads treat entries older than ``max_age`` as misses: eviction is opportunistic,
        so an idle or read-heavy cache would otherwise serve arbitrarily stale data."""
        if self.max_age is None:
            return None
        return now_utc() - timedelta(seconds=self.max_age)

    def _decode(self, data: bytes) -> Any:
        """Decode stored bytes; an undecodable entry reads as a miss (``_UNDECODABLE``) —
        a rotated encryption key or a coder change must not raise out of every read."""
        try:
            return self.coder.loads(data)
        except Exception:
            return _UNDECODABLE

    def _multi_read_stmt(self, hashes: list[int]):
        stmt = select(_entries.c.key, _entries.c.value).where(_entries.c.key_hash.in_(hashes))
        min_created = self._min_created_at()
        if min_created is not None:
            stmt = stmt.where(_entries.c.created_at >= min_created)
        return stmt

    def _read(self, fn: Callable[[Any], _T], miss: _T) -> _T:
        """Run a read query; a database failure degrades to ``miss`` instead of raising.

        Rails' cache is failure-safe: a dead cache DB turns reads into misses so the app keeps
        serving (slower), rather than 500-ing every request. firm mirrors that for *reads* only —
        writes still raise, since a write that silently no-ops is a worse surprise than an error.
        The failure is routed to ``on_error`` (not swallowed): a cache that has quietly stopped
        answering must stay observable.
        """
        try:
            with transaction(self.engine) as conn:
                return fn(conn)
        except SQLAlchemyError as exc:
            self.on_error(exc)
            return miss

    def get(self, key: str | bytes) -> Any | None:
        data = self._read(
            lambda conn: read_entry(conn, self._kb(key), min_created_at=self._min_created_at()),
            None,
        )
        if data is None:
            return None
        decoded = self._decode(data)
        return None if decoded is _UNDECODABLE else decoded

    def set(self, key: str | bytes, value: Any, *, unless_exist: bool = False) -> bool:
        """Store ``value`` under ``key``. With ``unless_exist=True`` write only when the key is
        absent; return ``True`` if the value was written, ``False`` if one was already there."""
        data = self.coder.dumps(value)
        kb = self._kb(key)
        with transaction(self.engine) as conn:
            if unless_exist:
                written = ensure_entry(conn, kb, data, self.encrypted)
            else:
                write_entry(conn, kb, data, self.encrypted)
                written = True
        if self.auto_expire and written:
            self.expiry.maybe_trigger(1)
        return written

    def fetch(
        self,
        key: str | bytes,
        default: Callable[[], Any] | Any,
        *,
        force: bool = False,
        skip_nil: bool = False,
    ) -> Any:
        # Decide hit/miss on row presence, not ``get() is not None`` — a stored ``None`` is a
        # hit (don't recompute it), exactly as a missing key is a miss. ``force`` bypasses the
        # read and always recomputes (cache-busting).
        if not force:
            with transaction(self.engine) as conn:
                data = read_entry(conn, self._kb(key), min_created_at=self._min_created_at())
            if data is not None:
                decoded = self._decode(data)
                if decoded is not _UNDECODABLE:
                    return decoded
        computed = default() if callable(default) else default
        # ``skip_nil`` leaves a computed ``None`` unstored, so the next fetch recomputes it.
        if not (skip_nil and computed is None):
            self.set(key, computed)
        return computed

    def delete(self, key: str | bytes) -> bool:
        with transaction(self.engine) as conn:
            return delete_entry(conn, self._kb(key))

    def delete_multi(self, keys: Iterable[str | bytes]) -> int:
        """Delete each key that exists; return how many were actually deleted."""
        deleted = 0
        with transaction(self.engine) as conn:
            for key in keys:
                if delete_entry(conn, self._kb(key)):
                    deleted += 1
        return deleted

    def exist(self, key: str | bytes) -> bool:
        return self._read(
            lambda conn: (
                read_entry(conn, self._kb(key), min_created_at=self._min_created_at()) is not None
            ),
            False,
        )

    def get_multi(self, keys: Iterable[str | bytes]) -> dict[Any, Any]:
        key_list = list(keys)
        kb_map = {key: self._kb(key) for key in key_list}
        hashes = [key_hash(kb) for kb in kb_map.values()]
        result: dict[Any, Any] = dict.fromkeys(key_list)
        rows = self._read(lambda conn: conn.execute(self._multi_read_stmt(hashes)).all(), [])
        by_key = {bytes(row.key): bytes(row.value) for row in rows}
        for key in key_list:
            data = by_key.get(kb_map[key])
            if data is not None:
                decoded = self._decode(data)
                if decoded is not _UNDECODABLE:
                    result[key] = decoded
        return result

    def set_multi(self, mapping: Mapping[str | bytes, Any]) -> None:
        with transaction(self.engine) as conn:
            for key, value in mapping.items():
                write_entry(conn, self._kb(key), self.coder.dumps(value), self.encrypted)
        if self.auto_expire:
            self.expiry.maybe_trigger(len(mapping))

    def fetch_multi(
        self, keys: Iterable[str | bytes], default: Callable[[Any], Any]
    ) -> dict[Any, Any]:
        """Return a value for every key: the cached one where present, else ``default(key)`` —
        which is computed, written back, and included. A stored ``None`` counts as a hit."""
        key_list = list(keys)
        kb_map = {key: self._kb(key) for key in key_list}
        hashes = [key_hash(kb) for kb in kb_map.values()]
        with transaction(self.engine) as conn:
            rows = conn.execute(self._multi_read_stmt(hashes)).all()
        by_kb = {bytes(row.key): bytes(row.value) for row in rows}
        result: dict[Any, Any] = {}
        to_write: dict[bytes, bytes] = {}
        for key in key_list:
            data = by_kb.get(kb_map[key])
            decoded = self._decode(data) if data is not None else _UNDECODABLE
            if decoded is not _UNDECODABLE:
                result[key] = decoded
            else:
                computed = default(key)
                result[key] = computed
                to_write[kb_map[key]] = self.coder.dumps(computed)
        if to_write:
            with transaction(self.engine) as conn:
                for kb, blob in to_write.items():
                    write_entry(conn, kb, blob, self.encrypted)
            if self.auto_expire:
                self.expiry.maybe_trigger(len(to_write))
        return result

    def increment(self, key: str | bytes, by: int = 1) -> int:
        kb = self._kb(key)
        zero = self.coder.dumps(0)
        # Serialize the read-modify-write: BEGIN IMMEDIATE on SQLite, SELECT ... FOR UPDATE on
        # Postgres/MySQL (after ensuring the row exists so there is something to lock).
        with immediate_transaction(self.engine) as conn:
            ensure_entry(conn, kb, zero, self.encrypted)
            data = read_entry_locked(conn, kb, min_created_at=self._min_created_at())
            current = 0
            if data is not None:
                decoded = self._decode(data)
                if decoded is not _UNDECODABLE:
                    current = int(decoded)
            new_value = current + by
            write_entry(conn, kb, self.coder.dumps(new_value), self.encrypted)
        return new_value

    def decrement(self, key: str | bytes, by: int = 1) -> int:
        return self.increment(key, -by)

    def clear(self) -> int:
        """Delete every entry; return how many rows were removed."""
        with transaction(self.engine) as conn:
            return conn.execute(delete(_entries)).rowcount

    def close(self) -> None:
        if self._loop is not None:
            self._loop.stop()
        self.expiry.shutdown()
        if self._owns_engine:
            dispose_engine(self.engine)

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
