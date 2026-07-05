# Configuration

Every `Channel(...)` option, with its default:

```python
Channel(
    database_url=None,         # SQLAlchemy URL (or pass engine=)
    engine=None,              # a pre-built SQLAlchemy Engine, instead of database_url
    polling_interval=0.1,     # seconds the listener sleeps between polls
    message_retention=86400.0,# trim messages older than this many seconds (1 day)
    autotrim=True,            # let broadcasts probabilistically trigger trimming
    trim_batch_size=100,      # rows per trim pass; also sets the trim-trigger rate (~2/N per write)
    create_schema=True,       # create firm_messages if missing
)
```

| Option | Default | Notes |
|---|---|---|
| `database_url` / `engine` | — | Provide one. `engine` lets several `Channel`es share a pool; bare `postgresql://`/`mysql://` URLs are normalized to the shipped drivers. |
| `polling_interval` | `0.1` | Lower = lower delivery latency, more queries; `0.1s` balances the two. |
| `message_retention` | `86400.0` | Age cut-off for trimming (seconds). |
| `autotrim` | `True` | Disable to trim only via `channel.trim()` / the CLI. |
| `trim_batch_size` | `100` | Trim batch + write-trigger rate (~`2/N`). |
| `create_schema` | `True` | Set `False` if you manage the schema with Alembic. |

Call `channel.close()` (or use the `with` form) to stop the listener and dispose the engine.

## Sharing an engine

```python
from firm._core.database import create_engine_for
engine = create_engine_for("postgresql://localhost/myapp")
pub = Channel(engine=engine)                       # a publisher
sub = Channel(engine=engine, create_schema=False)  # a subscriber sharing the pool
```

## Tuning latency vs. load

The listener polls every `polling_interval` seconds, so delivery latency is at most one interval.
Drop it (e.g. `0.05`) for snappier delivery at the cost of more `SELECT`s, or raise it on a busy
database where a little extra latency is fine.
