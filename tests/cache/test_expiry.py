"""Eviction specs — FIFO over-limit eviction and max-age expiry."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.cache import Cache, schema
from firm.cache.estimate import entry_count
from firm.cache.keys import key_hash, normalize_key


def _remaining_ids(cache: Cache) -> set[int]:
    with transaction(cache.engine) as conn:
        return {int(row.id) for row in conn.execute(select(schema.entries.c.id))}


def test_evicts_oldest_when_over_max_entries(db_url: str) -> None:
    cache = Cache(
        database_url=db_url,
        max_entries=5,
        max_size=None,
        max_age=None,
        expiry_batch_size=2,
        auto_expire=False,
    )
    try:
        for i in range(10):
            cache.set(f"k{i}", i)

        assert cache.expiry.run_once() == 2
        with transaction(cache.engine) as conn:
            assert entry_count(conn) == 8
        assert {7, 8, 9, 10}.issubset(_remaining_ids(cache))
    finally:
        cache.close()


def test_evicts_entries_older_than_max_age(db_url: str) -> None:
    cache = Cache(
        database_url=db_url,
        max_entries=None,
        max_size=None,
        max_age=3600.0,
        auto_expire=False,
    )
    try:
        cache.set("old", 1)
        cache.set("new", 2)
        with transaction(cache.engine) as conn:
            conn.execute(
                update(schema.entries)
                .where(schema.entries.c.key_hash == key_hash(normalize_key("old")))
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        assert cache.expiry.run_once() == 1
        assert cache.get("old") is None
        assert cache.get("new") == 2
    finally:
        cache.close()


def test_no_eviction_when_under_limits(db_url: str) -> None:
    cache = Cache(
        database_url=db_url, max_entries=100, max_size=None, max_age=None, auto_expire=False
    )
    try:
        cache.set("a", 1)
        assert cache.expiry.run_once() == 0
    finally:
        cache.close()
