"""Specs for the multi-part context + the cache/channel read & action layers."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, insert, select

from firm._core.clock import now_utc
from firm.cache import schema as cache_schema
from firm.channel import schema as channel_schema
from firm.channel.keys import channel_hash
from firm.ui import actions, audit_queries, cache_queries, channel_queries
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


def test_cache_stats_and_recent(runtime, seed) -> None:
    seed.cache_entry(key=b"a", value=b"1")
    seed.cache_entry(key=b"b", value=b"2")
    with runtime.engine.connect() as conn:
        stats = cache_queries.cache_stats(conn)
        recent = cache_queries.cache_recent(conn)
    assert stats["entries"] == 2
    assert stats["estimated_size"] > 0
    assert {e["key"] for e in recent} == {"a", "b"}


def test_cache_recent_paginates(runtime, seed) -> None:
    for i in range(30):
        seed.cache_entry(key=f"k{i:02d}".encode())
    with runtime.engine.connect() as conn:
        newest_first = [e["id"] for e in cache_queries.cache_recent(conn, limit=30)]
        page1 = cache_queries.cache_recent(conn, limit=10, offset=0)
        page2 = cache_queries.cache_recent(conn, limit=10, offset=10)
    assert [e["id"] for e in page1] == newest_first[0:10]
    assert [e["id"] for e in page2] == newest_first[10:20]


def test_clear_cache_action(runtime, seed) -> None:
    seed.cache_entry(key=b"a")
    seed.cache_entry(key=b"b")
    assert actions.clear_cache(runtime.engine) == 2
    with runtime.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(cache_schema.entries)).scalar() == 0


def test_channel_stats_top_and_recent(runtime, seed) -> None:
    seed.channel_message(channel=b"room:1", payload=b"x")
    seed.channel_message(channel=b"room:1", payload=b"y")
    seed.channel_message(channel=b"room:2", payload=b"z")
    with runtime.engine.connect() as conn:
        stats = channel_queries.channel_stats(conn)
        top = channel_queries.channel_top(conn)
        recent = channel_queries.channel_recent(conn)
    assert stats["messages"] == 3
    assert stats["channels"] == 2
    assert top[0]["channel"] == "room:1"  # busiest first
    assert top[0]["count"] == 2
    assert recent[0]["payload"] == "z"  # most recent first


def test_channel_top_paginates(runtime, seed) -> None:
    for i in range(30):
        # channel i gets (30 - i) messages, so channels are strictly busiest-first ordered
        for _ in range(30 - i):
            seed.channel_message(channel=f"room:{i:02d}".encode(), payload=b"x")
    with runtime.engine.connect() as conn:
        page1 = channel_queries.channel_top(conn, limit=10, offset=0)
        page2 = channel_queries.channel_top(conn, limit=10, offset=10)
    assert [t["channel"] for t in page1] == [f"room:{i:02d}" for i in range(0, 10)]
    assert [t["channel"] for t in page2] == [f"room:{i:02d}" for i in range(10, 20)]


def test_channel_recent_paginates(runtime, seed) -> None:
    for i in range(30):
        seed.channel_message(channel=b"room:1", payload=f"m{i:02d}".encode())
    with runtime.engine.connect() as conn:
        newest_first = [m["id"] for m in channel_queries.channel_recent(conn, limit=30)]
        page1 = channel_queries.channel_recent(conn, limit=10, offset=0)
        page2 = channel_queries.channel_recent(conn, limit=10, offset=10)
    assert [m["id"] for m in page1] == newest_first[0:10]
    assert [m["id"] for m in page2] == newest_first[10:20]


def test_trim_channel_action(runtime, seed) -> None:
    seed.channel_message(channel=b"room:1", payload=b"old", age_seconds=2 * 24 * 3600)
    seed.channel_message(channel=b"room:1", payload=b"new")
    assert actions.trim_channel(runtime.engine) == 1  # retention defaults to 1 day
    with runtime.engine.connect() as conn:
        remaining = conn.execute(select(channel_schema.messages.c.payload)).scalars().all()
    assert [bytes(p) for p in remaining] == [b"new"]


def test_audit_stats_and_search(runtime, seed) -> None:
    seed.audit_record(action="a", correlation_id="r1")
    seed.audit_record(action="b", correlation_id="r2")
    with runtime.engine.connect() as conn:
        stats = audit_queries.audit_stats(conn)
        rows = audit_queries.audit_search(conn, correlation_id="r1")
    assert stats["events"] == 2
    assert stats["actions"] == 2  # "a" and "b" are distinct
    assert stats["last_event_at"] is not None
    assert [r["action"] for r in rows] == ["a"]


def test_audit_stats_on_empty_table(runtime) -> None:
    with runtime.engine.connect() as conn:
        stats = audit_queries.audit_stats(conn)
    assert stats == {"events": 0, "actions": 0, "last_event_at": None}


def test_audit_search_sorts_by_each_column(runtime, seed) -> None:
    seed.audit_record(action="b.action", subject_type="Z", subject_id="1", correlation_id="c2")
    seed.audit_record(action="a.action", subject_type="A", subject_id="1", correlation_id="c1")
    with runtime.engine.connect() as conn:
        by_action_asc = audit_queries.audit_search(conn, sort="action", dir="asc")
        by_action_desc = audit_queries.audit_search(conn, sort="action", dir="desc")
        by_subject_asc = audit_queries.audit_search(conn, sort="subject", dir="asc")
    assert [r["action"] for r in by_action_asc] == ["a.action", "b.action"]
    assert [r["action"] for r in by_action_desc] == ["b.action", "a.action"]
    assert [r["subject_type"] for r in by_subject_asc] == ["A", "Z"]


def test_audit_search_falls_back_to_default_sort_for_unknown_key(runtime, seed) -> None:
    older = seed.audit_record(action="first")
    newer = seed.audit_record(action="second")
    with runtime.engine.connect() as conn:
        rows = audit_queries.audit_search(conn, sort="not-a-real-column")
    assert [r["id"] for r in rows] == [newer, older]  # falls back to created_at desc


def test_audit_search_paginates(runtime, seed) -> None:
    ids = [seed.audit_record(action=f"event.{i}") for i in range(30)]
    with runtime.engine.connect() as conn:
        page1 = audit_queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=0)
        page2 = audit_queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=10)
        page3 = audit_queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=20)
    assert [r["id"] for r in page1] == ids[0:10]
    assert [r["id"] for r in page2] == ids[10:20]
    assert [r["id"] for r in page3] == ids[20:30]


def test_audit_count_matches_filtered_search(runtime, seed) -> None:
    seed.audit_record(action="invoice.paid", correlation_id="r1")
    seed.audit_record(action="invoice.paid", correlation_id="r2")
    seed.audit_record(action="invoice.voided", correlation_id="r3")
    with runtime.engine.connect() as conn:
        assert audit_queries.audit_count(conn) == 3
        assert audit_queries.audit_count(conn, action="invoice.paid") == 2
        assert audit_queries.audit_count(conn, action="invoice.voided") == 1
        assert audit_queries.audit_count(conn, correlation_id="r1") == 1
        assert audit_queries.audit_count(conn, action="nonexistent") == 0


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
