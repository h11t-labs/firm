"""Out-of-id-order commits must not lose messages (PLAN 1.1 / audit CH-1).

On Postgres/MySQL, autoincrement ids are assigned at INSERT but become visible at COMMIT, so
commit order != id order under concurrent broadcasters. A max-id watermark would skip a lower
id that commits after a higher one was already seen. The listener therefore re-scans a bounded
window above its floor and de-duplicates deliveries.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import insert

from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm.channel import Channel, schema
from firm.channel.keys import channel_hash, normalize_channel


def _insert_with_id(conn, message_id: int, channel: bytes, payload: bytes) -> None:
    conn.execute(
        insert(schema.messages).values(
            id=message_id,
            channel=channel,
            payload=payload,
            channel_hash=channel_hash(channel),
            created_at=now_utc(),
        )
    )


def test_lower_id_appearing_after_higher_id_is_still_delivered(
    channel: Channel, wait_for: Callable
) -> None:
    """Logic-level simulation (runs on every backend): a row with a lower id that becomes
    visible after a higher id was already dispatched must still be delivered."""
    received: list[bytes] = []
    channel.subscribe("room", received.append)
    ch = normalize_channel("room")

    # A high id lands first (as if its transaction committed first)...
    with channel.engine.begin() as conn:
        _insert_with_id(conn, 100, ch, b"first-visible")
    assert wait_for(lambda: b"first-visible" in received)

    # ...then a lower id becomes visible (its transaction committed late). The old max-id
    # watermark had already moved past it; the re-scan window must pick it up.
    with channel.engine.begin() as conn:
        _insert_with_id(conn, 50, ch, b"late-commit")
    assert wait_for(lambda: b"late-commit" in received)

    # And re-scanning must not double-deliver what was already dispatched.
    assert received.count(b"first-visible") == 1
    assert received.count(b"late-commit") == 1


def test_concurrent_uncommitted_broadcast_is_delivered_after_commit(
    channel: Channel, db_url: str, wait_for: Callable
) -> None:
    """The real interleaving, with two raw connections (Postgres/MySQL only: SQLite's single
    writer serializes the two inserts, so the race cannot occur there by construction)."""
    if db_url.startswith("sqlite"):
        import pytest

        pytest.skip("SQLite serializes writers; the out-of-order commit cannot happen")

    received: list[bytes] = []
    channel.subscribe("race", received.append)
    ch = normalize_channel("race")

    eng_a = create_engine_for(db_url)
    eng_b = create_engine_for(db_url)
    try:
        conn_a = eng_a.connect()
        tx_a = conn_a.begin()
        # A takes the lower id but does not commit yet.
        _insert_with_id_auto = insert(schema.messages).values(
            channel=ch,
            payload=b"held-open",
            channel_hash=channel_hash(ch),
            created_at=now_utc(),
        )
        conn_a.execute(_insert_with_id_auto)

        # B takes the higher id and commits immediately; the listener sees it first.
        with eng_b.begin() as conn_b:
            conn_b.execute(
                insert(schema.messages).values(
                    channel=ch,
                    payload=b"committed-first",
                    channel_hash=channel_hash(ch),
                    created_at=now_utc(),
                )
            )
        assert wait_for(lambda: b"committed-first" in received)
        assert b"held-open" not in received

        # A finally commits: with a max-id watermark this message was lost forever.
        tx_a.commit()
        conn_a.close()
        assert wait_for(lambda: b"held-open" in received)
        assert received.count(b"held-open") == 1
        assert received.count(b"committed-first") == 1
    finally:
        eng_a.dispose()
        eng_b.dispose()
