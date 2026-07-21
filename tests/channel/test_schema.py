"""Schema specs for the channel table + indexes."""

from __future__ import annotations

from sqlalchemy import inspect

from firm.channel import Channel


def test_messages_table_and_indexes_created(channel: Channel) -> None:
    insp = inspect(channel.engine)
    assert "firm_channel_messages" in insp.get_table_names()
    index_names = {ix["name"] for ix in insp.get_indexes("firm_channel_messages")}
    assert "index_firm_channel_messages_on_channel" in index_names
    assert "index_firm_channel_messages_on_channel_hash" in index_names
    assert "index_firm_channel_messages_on_created_at" in index_names
