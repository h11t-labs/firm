"""Size-estimation specs."""

from __future__ import annotations

from sqlalchemy import func, select

from firm._core.database import transaction
from firm.cache import Cache, schema
from firm.cache.estimate import entry_count, estimate_size


def test_exact_size_for_small_table(cache: Cache) -> None:
    cache.set("a", "x")
    cache.set("b", "yy")
    with transaction(cache.engine) as conn:
        real = conn.execute(select(func.sum(schema.entries.c.byte_size))).scalar()
        estimate = estimate_size(conn, samples=10000)
    assert estimate == real
    assert estimate > 0


def test_entry_count(cache: Cache) -> None:
    cache.set("a", 1)
    cache.set("b", 2)
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 2


def test_empty_cache_estimates_zero(cache: Cache) -> None:
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 0
        assert estimate_size(conn) == 0
