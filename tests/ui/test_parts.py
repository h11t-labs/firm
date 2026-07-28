"""Specs for the multi-part dashboard context + the cache/channel action layers.

The read layers themselves live in the owning packages now; their specs are in
``tests/{queue,cache,channel,audit}/test_queries.py``.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, insert, select

from firm._core.clock import now_utc
from firm.cache import schema as cache_schema
from firm.channel import schema as channel_schema
from firm.channel.keys import channel_hash
from firm.ui import actions
from firm.ui.context import build_dashboard


def test_dashboard_enables_present_parts(runtime, db_url) -> None:
    dash = build_dashboard(database_url=db_url)
    try:
        assert dash.parts == ["queue", "cache", "channel", "audit"]
    finally:
        dash.close()


def test_dashboard_empty_when_no_tables(tmp_path) -> None:
    dash = build_dashboard(database_url=f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        assert dash.parts == []
    finally:
        dash.close()


def test_clear_cache_action(runtime, seed) -> None:
    seed.cache_entry(key=b"a")
    seed.cache_entry(key=b"b")
    assert actions.clear_cache(runtime.engine) == 2
    with runtime.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(cache_schema.entries)).scalar() == 0


def test_trim_channel_action(runtime, seed) -> None:
    seed.channel_message(channel=b"room:1", payload=b"old", age_seconds=2 * 24 * 3600)
    seed.channel_message(channel=b"room:1", payload=b"new")
    assert actions.trim_channel(runtime.engine) == 1  # retention defaults to 1 day
    with runtime.engine.connect() as conn:
        remaining = conn.execute(select(channel_schema.messages.c.payload)).scalars().all()
    assert [bytes(p) for p in remaining] == [b"new"]


def test_trim_channel_sweeps_past_one_batch(runtime) -> None:
    # More than one trim_batch_size (default 100) of expired rows: one click must clear them all,
    # not just the first batch.
    old = now_utc() - timedelta(days=2)
    chash = channel_hash(b"c")
    rows = [
        {"channel": b"c", "payload": b"old", "channel_hash": chash, "created_at": old}
        for _ in range(105)
    ]
    fresh = {"channel": b"c", "payload": b"fresh", "channel_hash": chash, "created_at": now_utc()}
    with runtime.engine.begin() as conn:
        conn.execute(insert(channel_schema.messages), [*rows, fresh])
    assert actions.trim_channel(runtime.engine) == 105  # both batches swept in one call
    with runtime.engine.connect() as conn:
        remaining = conn.execute(select(func.count()).select_from(channel_schema.messages)).scalar()
    assert remaining == 1  # only the fresh message survives
