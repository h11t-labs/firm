"""Reads must never serve entries older than ``max_age`` (audit C-2).

Eviction is opportunistic (probabilistic on writes, or an opt-in background loop), so an idle
or read-heavy cache used to serve arbitrarily stale data forever. Entries are backdated via
SQL so the tests are deterministic — no sleeping on real TTLs.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from firm._core.clock import now_utc
from firm.cache import Cache, schema


def _backdate(cache: Cache, seconds: float) -> None:
    with cache.engine.begin() as conn:
        conn.execute(
            update(schema.entries).values(created_at=now_utc() - timedelta(seconds=seconds))
        )


def test_reads_treat_entries_older_than_max_age_as_misses(db_url) -> None:
    with Cache(database_url=db_url, max_age=3600, auto_expire=False) as cache:
        cache.set("k", "v")
        assert cache.get("k") == "v"
        assert cache.exist("k") is True

        _backdate(cache, 2 * 3600)

        assert cache.get("k") is None
        assert cache.exist("k") is False
        assert cache.get_multi(["k"]) == {"k": None}


def test_fetch_recomputes_an_aged_out_entry(db_url) -> None:
    with Cache(database_url=db_url, max_age=3600, auto_expire=False) as cache:
        cache.set("k", "stale")
        _backdate(cache, 2 * 3600)

        assert cache.fetch("k", lambda: "fresh") == "fresh"
        # The write-back refreshed created_at, so the recomputed value is served again.
        assert cache.get("k") == "fresh"

        cache.set("m", "old-multi")
        _backdate(cache, 2 * 3600)
        assert cache.fetch_multi(["m"], lambda key: "fresh-multi") == {"m": "fresh-multi"}
        assert cache.get("m") == "fresh-multi"


def test_increment_resets_an_aged_out_counter(db_url) -> None:
    with Cache(database_url=db_url, max_age=3600, auto_expire=False) as cache:
        assert cache.increment("counter") == 1
        assert cache.increment("counter") == 2

        _backdate(cache, 2 * 3600)

        # The expired value must not leak into the new count.
        assert cache.increment("counter") == 1


def test_no_max_age_means_reads_never_expire(db_url) -> None:
    with Cache(database_url=db_url, max_age=None, auto_expire=False) as cache:
        cache.set("k", "v")
        _backdate(cache, 10 * 365 * 24 * 3600)
        assert cache.get("k") == "v"
