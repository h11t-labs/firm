"""Specs for the read-query surface (:mod:`firm.channel.queries`)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import insert

from firm._core.clock import now_utc
from firm.channel import Channel, queries, schema
from firm.channel.keys import channel_hash


def message(
    channel: Channel,
    *,
    name: bytes = b"room:1",
    payload: bytes = b"hello",
    age_seconds: float = 0.0,
) -> None:
    """Buffer one message directly, so the read layer can be exercised without a subscriber."""
    with channel.engine.begin() as conn:
        conn.execute(
            insert(schema.messages).values(
                channel=name,
                payload=payload,
                channel_hash=channel_hash(name),
                created_at=now_utc() - timedelta(seconds=age_seconds),
            )
        )


def test_channel_stats_top_and_recent(channel: Channel) -> None:
    message(channel, name=b"room:1", payload=b"x")
    message(channel, name=b"room:1", payload=b"y")
    message(channel, name=b"room:2", payload=b"z")
    with channel.engine.connect() as conn:
        stats = queries.channel_stats(conn)
        top = queries.channel_top(conn)
        recent = queries.channel_recent(conn)
    assert stats["messages"] == 3
    assert stats["channels"] == 2
    assert top[0]["channel"] == b"room:1"  # busiest first
    assert top[0]["count"] == 2
    assert recent[0]["payload"] == b"z"  # most recent first


def test_channel_names_and_payloads_are_bytes(channel: Channel) -> None:
    # The contract is bytes on every backend — MySQL hands the driver's memoryview back otherwise.
    message(channel, name=b"room:1", payload=b"x")
    with channel.engine.connect() as conn:
        (top,) = queries.channel_top(conn)
        (recent,) = queries.channel_recent(conn)
    assert type(top["channel"]) is bytes
    assert type(recent["channel"]) is bytes
    assert type(recent["payload"]) is bytes


def test_channel_top_paginates(channel: Channel) -> None:
    for i in range(30):
        # channel i gets (30 - i) messages, so channels are strictly busiest-first ordered
        for _ in range(30 - i):
            message(channel, name=f"room:{i:02d}".encode(), payload=b"x")
    with channel.engine.connect() as conn:
        page1 = queries.channel_top(conn, limit=10, offset=0)
        page2 = queries.channel_top(conn, limit=10, offset=10)
    assert [t["channel"] for t in page1] == [f"room:{i:02d}".encode() for i in range(0, 10)]
    assert [t["channel"] for t in page2] == [f"room:{i:02d}".encode() for i in range(10, 20)]


def test_channel_recent_paginates(channel: Channel) -> None:
    for i in range(30):
        message(channel, name=b"room:1", payload=f"m{i:02d}".encode())
    with channel.engine.connect() as conn:
        newest_first = [m["id"] for m in queries.channel_recent(conn, limit=30)]
        page1 = queries.channel_recent(conn, limit=10, offset=0)
        page2 = queries.channel_recent(conn, limit=10, offset=10)
    assert [m["id"] for m in page1] == newest_first[0:10]
    assert [m["id"] for m in page2] == newest_first[10:20]


def test_reads_reject_a_negative_window(channel: Channel) -> None:
    with channel.engine.connect() as conn:
        with pytest.raises(ValueError, match="limit"):
            queries.channel_top(conn, limit=-1)
        with pytest.raises(ValueError, match="offset"):
            queries.channel_top(conn, offset=-1)
        with pytest.raises(ValueError, match="limit"):
            queries.channel_recent(conn, limit=-1)
        with pytest.raises(ValueError, match="offset"):
            queries.channel_recent(conn, offset=-1)
