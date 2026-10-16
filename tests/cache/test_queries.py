"""Specs for the read-query surface (:mod:`firm.cache.queries`)."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from firm._core.clock import now_utc
from firm.cache import Cache, queries, schema
from firm.cache.keys import key_hash


def cache_entry(cache: Cache, *, key: bytes = b"user:1", value: bytes = b"payload") -> None:
    """Insert one entry directly — the read layer never decodes the value, so a plain Core insert
    is enough and keeps the row shape explicit."""
    with cache.engine.begin() as conn:
        conn.execute(
            insert(schema.entries).values(
                key=key,
                value=value,
                key_hash=key_hash(key),
                byte_size=len(key) + len(value) + 140,
                created_at=now_utc(),
            )
        )


def test_cache_stats_and_recent(cache: Cache) -> None:
    cache_entry(cache, key=b"a", value=b"1")
    cache_entry(cache, key=b"b", value=b"2")
    with cache.engine.connect() as conn:
        stats = queries.cache_stats(conn)
        recent = queries.cache_recent(conn)
    assert stats["entries"] == 2
    assert stats["estimated_size"] > 0
    assert {e["key"] for e in recent} == {b"a", b"b"}


def test_cache_recent_returns_keys_as_bytes(cache: Cache) -> None:
    # The contract is bytes on every backend — MySQL hands the driver's memoryview back otherwise.
    cache_entry(cache, key=b"a")
    with cache.engine.connect() as conn:
        (entry,) = queries.cache_recent(conn)
    assert type(entry["key"]) is bytes


def test_cache_recent_paginates(cache: Cache) -> None:
    for i in range(30):
        cache_entry(cache, key=f"k{i:02d}".encode())
    with cache.engine.connect() as conn:
        newest_first = [e["id"] for e in queries.cache_recent(conn, limit=30)]
        page1 = queries.cache_recent(conn, limit=10, offset=0)
        page2 = queries.cache_recent(conn, limit=10, offset=10)
    assert [e["id"] for e in page1] == newest_first[0:10]
    assert [e["id"] for e in page2] == newest_first[10:20]


def test_cache_recent_rejects_a_negative_window(cache: Cache) -> None:
    with cache.engine.connect() as conn:
        with pytest.raises(ValueError, match="limit"):
            queries.cache_recent(conn, limit=-1)
        with pytest.raises(ValueError, match="offset"):
            queries.cache_recent(conn, offset=-1)
