"""Trimming / retention specs."""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import insert, select

from firm._core.clock import now_utc
from firm.channel import Channel, schema

_messages = schema.messages


def _insert(channel: Channel, payload: bytes, age_seconds: float) -> None:
    with channel.engine.begin() as conn:
        conn.execute(
            insert(_messages).values(
                channel=b"c",
                payload=payload,
                channel_hash=1,
                created_at=now_utc() - timedelta(seconds=age_seconds),
            )
        )


def test_trim_deletes_old_keeps_recent(channel: Channel) -> None:
    _insert(channel, b"old", age_seconds=2 * 24 * 3600)  # 2 days
    _insert(channel, b"new", age_seconds=0)
    assert channel.trim() == 1  # retention defaults to 1 day
    with channel.engine.connect() as conn:
        remaining = conn.execute(select(_messages.c.payload)).scalars().all()
    assert [bytes(p) for p in remaining] == [b"new"]


def test_trim_respects_batch_size(channel: Channel) -> None:
    channel.trim_batch_size = 2
    for _ in range(5):
        _insert(channel, b"x", age_seconds=2 * 24 * 3600)
    assert channel.trim() == 2
    assert channel.trim() == 2
    assert channel.trim() == 1
    assert channel.trim() == 0


def test_trim_noop_when_all_recent(channel: Channel) -> None:
    _insert(channel, b"a", age_seconds=0)
    _insert(channel, b"b", age_seconds=0)
    assert channel.trim() == 0


def test_auto_trim_triggers_trim_on_broadcast(db_url: str) -> None:
    # trim_batch_size=2 -> expected trims per write = (1/2) * TRIM_MULTIPLIER(2) = 1.0, so exactly
    # one trim is submitted per broadcast with no randomness — making the auto_trim path testable.
    ps = Channel(database_url=db_url, auto_trim=True, trim_batch_size=2)
    try:
        with ps.engine.begin() as conn:
            conn.execute(
                insert(_messages).values(
                    channel=b"c",
                    payload=b"old",
                    channel_hash=1,
                    created_at=now_utc() - timedelta(days=2),
                )
            )
        ps.broadcast("c", b"new")  # auto_trim submits one async trim onto the background pool
        deadline = time.monotonic() + 2.0
        payloads: list[bytes] = []
        while time.monotonic() < deadline:
            with ps.engine.connect() as conn:
                payloads = [bytes(p) for p in conn.execute(select(_messages.c.payload)).scalars()]
            if b"old" not in payloads:
                break
            time.sleep(0.02)
        assert b"old" not in payloads  # the aged-out row was trimmed by the auto_trim run
        assert b"new" in payloads  # the fresh broadcast survived
    finally:
        ps.close()
