"""The ``Cache`` — a database-backed key/value store."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, select

from .._core.clock import now_utc
from .._core.database import (
    create_engine_for,
    dispose_engine,
    immediate_transaction,
    transaction,
)
from . import schema
from .entries import delete_entry, ensure_entry, read_entry, read_entry_locked, write_entry
from .expiry import Expiry, ExpiryLoop
from .keys import key_hash, normalize_key
from .serialization import Coder, PickleCoder, build_encrypted_coder

_entries = schema.entries

TWO_WEEKS_SECONDS = 14 * 24 * 3600.0
DEFAULT_MAX_SIZE = 256 * 1024 * 1024


class Cache:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        coder: Coder | None = None,
        encrypt_key: str | bytes | None = None,
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
    ) -> None:
        if engine is not None:
            self.engine = engine
            self._owns_engine = False
        elif database_url is not None:
            self.engine = create_engine_for(database_url)
            self._owns_engine = True
        else:
            raise ValueError("Cache requires either database_url or engine")

        base: Coder = coder or PickleCoder()
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

    def _multi_read_stmt(self, hashes: list[int]):
        stmt = select(_entries.c.key, _entries.c.value).where(_entries.c.key_hash.in_(hashes))
        min_created = self._min_created_at()
        if min_created is not None:
            stmt = stmt.where(_entries.c.created_at >= min_created)
        return stmt

    def get(self, key: str | bytes) -> Any | None:
        with transaction(self.engine) as conn:
            data = read_entry(conn, self._kb(key), min_created_at=self._min_created_at())
        return None if data is None else self.coder.loads(data)

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

    def fetch(self, key: str | bytes, default: Callable[[], Any] | Any) -> Any:
        # Decide hit/miss on row presence, not ``get() is not None`` — a stored ``None`` is a
        # hit (don't recompute it), exactly as a missing key is a miss.
        with transaction(self.engine) as conn:
            data = read_entry(conn, self._kb(key), min_created_at=self._min_created_at())
        if data is not None:
            return self.coder.loads(data)
        computed = default() if callable(default) else default
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
        with transaction(self.engine) as conn:
            return (
                read_entry(conn, self._kb(key), min_created_at=self._min_created_at()) is not None
            )

    def get_multi(self, keys: Iterable[str | bytes]) -> dict[Any, Any]:
        key_list = list(keys)
        kb_map = {key: self._kb(key) for key in key_list}
        hashes = [key_hash(kb) for kb in kb_map.values()]
        result: dict[Any, Any] = dict.fromkeys(key_list)
        with transaction(self.engine) as conn:
            rows = conn.execute(self._multi_read_stmt(hashes)).all()
        by_key = {bytes(row.key): bytes(row.value) for row in rows}
        for key in key_list:
            data = by_key.get(kb_map[key])
            if data is not None:
                result[key] = self.coder.loads(data)
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
            if data is not None:
                result[key] = self.coder.loads(data)
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
            current = int(self.coder.loads(data)) if data is not None else 0
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
