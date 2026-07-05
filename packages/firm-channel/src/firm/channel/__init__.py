"""firm-channel — database-backed publish/subscribe for Python.

from firm.channel import Channel

ps = Channel(database_url="sqlite:///channel.db")
ps.subscribe("room:42", lambda payload: print(payload))
ps.broadcast("room:42", b'{"hello": "world"}')
"""

from __future__ import annotations

from .channel import Channel

__version__ = "0.1.0"

__all__ = ["Channel", "__version__"]
