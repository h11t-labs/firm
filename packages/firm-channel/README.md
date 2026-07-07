# firm-channel

Database-backed publish/subscribe for Python — a pure-Python port of Rails'
[Solid Cable](https://github.com/rails/solid_cable). No Redis required: messages flow through
your existing **SQLite**, **PostgreSQL**, or **MySQL/MariaDB** database.

Part of [firm](https://github.com/h11t-labs/firm), a port of the Rails 8 Solid stack.

```bash
pip install firm-channel          # or: pip install "firm[channel]"
```

## Quickstart

```python
from firm.channel import Channel

ps = Channel(database_url="postgresql://localhost/myapp")

ps.subscribe("room:42", lambda payload: print(payload))
ps.broadcast("room:42", b'{"msg": "hi"}')
```

## Highlights

- **Broadcast/subscribe** over the database — no extra broker to run
- **Polling listener** with configurable interval, in the style of Solid Cable
- **Automatic message trimming** with configurable retention

## Docs

- [Channel overview](https://github.com/h11t-labs/firm/blob/main/docs/channel/overview.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed. Schema and design derived from Solid Cable (© 37signals, MIT); see
[NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE).
