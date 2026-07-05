# Getting started

## Install

```bash
pip install "firm[channel]"             # or: uv add "firm[channel]"
pip install "firm[channel,postgres]"    # psycopg, for PostgreSQL
pip install "firm[channel,mysql]"       # PyMySQL, for MySQL/MariaDB
```

See **[Installation](../installation.md)** for the full list of extras.

## Create a Channel

```python
from firm.channel import Channel

ps = Channel(database_url="sqlite:///channel.db")
```

By default `Channel(...)` creates the `firm_messages` table if it's missing
(`create_schema=True`). For production schema management, use the bundled Alembic migration and pass
`create_schema=False` — see [Database backends](../database-backends.md#migrations).

## Subscribe and broadcast

```python
def on_message(payload: bytes) -> None:
    print("got", payload)

ps.subscribe("room:42", on_message)   # start listening (spins up the background listener)
ps.broadcast("room:42", b"hello")     # on_message fires on the next poll (~0.1s)

ps.broadcast("room:42", "héllo")      # a str payload is UTF-8 encoded -> b"h\xc3\xa9llo"
ps.unsubscribe("room:42", on_message) # stop listening
```

Payloads are opaque bytes. To send structured data, serialize it yourself:

```python
import json
ps.broadcast("room:42", json.dumps({"user": "ada", "text": "hi"}).encode())
```

## Clean up

A `Channel` owns a connection pool and (once you subscribe) a background listener thread. Close it
when you're done, or use it as a context manager:

```python
with Channel(database_url="sqlite:///channel.db") as ps:
    ps.subscribe("room:42", on_message)
    ps.broadcast("room:42", b"hi")
# listener stopped, engine disposed
```

## A complete example

```python
import time
from firm.channel import Channel

with Channel(database_url="sqlite:///channel.db") as ps:
    received: list[bytes] = []
    ps.subscribe("news", received.append)
    ps.broadcast("news", b"first")
    ps.broadcast("news", b"second")
    time.sleep(0.3)                 # let the listener poll
    print(received)                 # [b"first", b"second"]
```

Next: **[Configuration](configuration.md)** and **[Internals](internals.md)**.
