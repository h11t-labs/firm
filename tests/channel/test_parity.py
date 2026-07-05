"""Parity tests ported from rails/solid_cable.

Each test cites its upstream counterpart. They mirror ``test_channel.py``: subscribe a callback
that appends to a list, broadcast, then assert delivery via ``wait_for`` (the ``channel`` fixture
polls every 0.01s with autotrim off, keeping delivery fast and deterministic).
"""

from __future__ import annotations

from collections.abc import Callable

import firm.channel.messages as messages_mod
from firm import channel as channel_pkg
from firm.channel import Channel
from firm.channel.channel import DEFAULT_TRIM_BATCH_SIZE


# Upstream: test/adapters/*_adapter_test.rb shared example "long_identifiers".
# Broadcast/subscribe on a long channel name, and two near-identical long names (differing only in
# the final char) each deliver only their own message. Stresses the VARBINARY(1024) channel column
# width and the channel_hash routing.
def test_long_identifiers(channel: Channel, wait_for: Callable) -> None:
    long_name = "channel:" + ("a" * 116)  # ~124 chars, well under the 1024-byte column
    assert len(long_name) > 120

    received: list[bytes] = []
    channel.subscribe(long_name, received.append)
    channel.broadcast(long_name, b"payload")
    assert wait_for(lambda: received == [b"payload"])


def test_long_identifiers_near_identical_names_do_not_cross(
    channel: Channel, wait_for: Callable
) -> None:
    base = "channel:" + ("a" * 116)
    name_a = base + "X"  # differ only in the last character
    name_b = base + "Y"
    assert name_a != name_b and name_a[:-1] == name_b[:-1] and len(name_a) > 120

    a: list[bytes] = []
    b: list[bytes] = []
    channel.subscribe(name_a, a.append)
    channel.subscribe(name_b, b.append)
    channel.broadcast(name_a, b"for-a")
    channel.broadcast(name_b, b"for-b")
    assert wait_for(lambda: a == [b"for-a"] and b == [b"for-b"])
    # Neither long channel leaks into the other.
    assert b"for-b" not in a
    assert b"for-a" not in b


# Upstream: test/adapters/*_adapter_test.rb shared example
# "does not raise error when polling with no Active Record logger".
# firm uses no logging; ported as a smoke test that a full subscribe -> broadcast -> receive cycle
# completes without error (guards the poll/dispatch path).
def test_full_cycle_completes_without_error(channel: Channel, wait_for: Callable) -> None:
    received: list[bytes] = []
    channel.subscribe("smoke", received.append)
    channel.broadcast("smoke", b"ok")
    assert wait_for(lambda: received == [b"ok"])


# Upstream: test/adapters/*_adapter_test.rb shared example
# "retries after a connection failure and keeps listening".
# Make the listener's poll cycle hit a transient error ONCE, then recover, and assert a broadcast
# AFTER the failure is still delivered (proving the listener thread did not die). The poller loop in
# _core/poller.py catches poll() exceptions and continues; _dispatch_new calls
# messages.fetch_since, so wrapping that is the right seam.
def test_retries_after_failure_and_keeps_listening(
    channel: Channel, wait_for: Callable, monkeypatch
) -> None:
    real_fetch_since = messages_mod.fetch_since
    calls = {"n": 0}

    def flaky_fetch_since(conn, channel_hashes, after_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient connection failure")
        return real_fetch_since(conn, channel_hashes, after_id)

    monkeypatch.setattr(messages_mod, "fetch_since", flaky_fetch_since)

    received: list[bytes] = []
    channel.subscribe("retry", received.append)
    channel.broadcast("retry", b"after-failure")

    # The first poll raises; a later poll must still deliver the message.
    assert wait_for(lambda: received == [b"after-failure"])
    assert calls["n"] >= 2  # confirm the listener actually retried past the failing call

    monkeypatch.undo()  # restore the real fetch_since


# Upstream: test/solid_cable_test.rb "it has a version number".
def test_has_version_number() -> None:
    assert isinstance(channel_pkg.__version__, str)
    assert channel_pkg.__version__ != ""


# Upstream: test/solid_cable_test.rb "default trim_batch_size is 100".
def test_default_trim_batch_size_is_100(db_url: str) -> None:
    assert DEFAULT_TRIM_BATCH_SIZE == 100
    # The Channel constructor default mirrors the module constant.
    ch = Channel(database_url=db_url)
    try:
        assert ch.trim_batch_size == 100
    finally:
        ch.close()
