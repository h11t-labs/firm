"""Broadcast / subscribe behavior specs."""

from __future__ import annotations

import time
from collections.abc import Callable

from firm.channel import Channel


def test_broadcast_subscribe_roundtrip(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    channel.subscribe("room:1", received.append)
    channel.broadcast("room:1", b"hello")
    assert wait_for(lambda: received == [b"hello"])


def test_string_payload_is_utf8_encoded(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    channel.subscribe("room", received.append)
    channel.broadcast("room", "héllo")
    assert wait_for(lambda: received == ["héllo".encode()])


def test_only_subscribed_channel_receives(channel: Channel, wait_for: Callable) -> None:
    a: list[bytes] = []
    b: list[bytes] = []
    channel.subscribe("a", a.append)
    channel.subscribe("b", b.append)
    channel.broadcast("a", b"x")
    assert wait_for(lambda: a == [b"x"])
    assert b == []


def test_multiple_subscribers_same_channel(channel: Channel, wait_for: Callable) -> None:
    first: list[bytes] = []
    second: list[bytes] = []
    channel.subscribe("c", first.append)
    channel.subscribe("c", second.append)
    channel.broadcast("c", b"y")
    assert wait_for(lambda: first == [b"y"] and second == [b"y"])


def test_unsubscribe_removes_registered_callback(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    callback = received.append
    channel.subscribe("d", callback)
    channel.unsubscribe("d", callback)
    channel.broadcast("d", b"z")
    # Nothing should ever arrive; confirm the channel stays empty across several poll cycles.
    assert not wait_for(lambda: received != [], timeout=0.3)


def test_messages_before_subscribe_are_not_delivered(channel: Channel, wait_for: Callable) -> None:
    channel.broadcast("e", b"old")
    time.sleep(0.05)
    received: list[bytes] = []
    channel.subscribe("e", received.append)
    channel.broadcast("e", b"new")
    assert wait_for(lambda: received == [b"new"])


def test_ordered_delivery(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    channel.subscribe("f", received.append)
    for i in range(5):
        channel.broadcast("f", str(i).encode())
    assert wait_for(lambda: len(received) == 5)
    assert received == [b"0", b"1", b"2", b"3", b"4"]


def test_backlog_on_quiet_channel_not_replayed_to_later_subscriber(
    channel: Channel, wait_for: Callable
) -> None:
    # The listener is running (subscribed to "a"), but "b" has no subscriber yet.
    channel.subscribe("a", [].append)
    channel.broadcast("b", b"old-on-b")
    time.sleep(0.05)  # let the row commit and a poll cycle go by
    # Subscribing to "b" now must NOT replay the message broadcast before the subscription.
    received: list[bytes] = []
    channel.subscribe("b", received.append)
    channel.broadcast("b", b"new-on-b")
    assert wait_for(lambda: received == [b"new-on-b"])
    assert b"old-on-b" not in received


def test_resubscribe_does_not_replay_gap_messages(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    callback = received.append
    channel.subscribe("z", callback)
    channel.broadcast("z", b"m1")
    assert wait_for(lambda: received == [b"m1"])
    channel.unsubscribe("z", callback)
    channel.broadcast("z", b"gap")  # broadcast while "z" has no subscriber
    time.sleep(0.05)
    received.clear()
    channel.subscribe("z", callback)
    channel.broadcast("z", b"m2")
    assert wait_for(lambda: received == [b"m2"])
    assert b"gap" not in received


def test_subscriber_error_does_not_break_listener(channel: Channel, wait_for: Callable) -> None:
    def boom(_payload: bytes) -> None:
        raise RuntimeError("subscriber blew up")

    good: list[bytes] = []
    channel.subscribe("e", boom)
    channel.subscribe("e", good.append)
    channel.broadcast("e", b"1")
    channel.broadcast("e", b"2")
    # The raising callback must not stop the sibling callback or the listener.
    assert wait_for(lambda: good == [b"1", b"2"])


def test_channel_hash_collision_dispatches_by_exact_channel(
    channel: Channel, wait_for: Callable, monkeypatch
) -> None:
    # Force every channel to share one hash, then confirm a colliding-but-different channel's
    # message is fetched (by hash) yet never delivered (dispatch is by exact channel bytes).
    import firm.channel.channel as channel_mod
    import firm.channel.messages as messages_mod

    monkeypatch.setattr(messages_mod, "channel_hash", lambda _channel: 12345)
    monkeypatch.setattr(channel_mod, "channel_hash", lambda _channel: 12345)

    received: list[bytes] = []
    channel.subscribe("A", received.append)
    channel.broadcast("B", b"for-B")  # same forced hash as "A", different channel
    channel.broadcast("A", b"for-A")
    assert wait_for(lambda: received == [b"for-A"])
    assert b"for-B" not in received


def test_listener_errors_reach_on_error(db_url, wait_for) -> None:
    """X-1: listener poll failures were dropped by the poller default; they now route to
    Channel(on_error=...)."""
    from unittest import mock

    seen: list[BaseException] = []
    ps = Channel(database_url=db_url, polling_interval=0.01, autotrim=False, on_error=seen.append)
    try:
        ps.subscribe("room", lambda payload: None)
        with mock.patch(
            "firm.channel.channel.messages.fetch_since",
            side_effect=RuntimeError("listener-fail"),
        ):
            ps.broadcast("room", b"x")
            assert wait_for(lambda: any("listener-fail" in str(e) for e in seen))
    finally:
        ps.close()
