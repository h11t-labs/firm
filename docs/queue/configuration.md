# Configuration

## `configure()`

Set the process-global runtime once at startup:

```python
import firm.queue as bq

bq.configure(
    database_url="postgresql://localhost/myapp",
    busy_timeout_ms=5000,
    default_queue="default",
    preserve_finished_jobs=True,
)
```

| Setting | Default | Meaning |
|---|---|---|
| `database_url` | — | SQLAlchemy URL. Bare `postgresql://` / `mysql://` are normalized to the shipped drivers. |
| `engine` | `None` | Share your application's SQLAlchemy engine instead of a URL (you keep ownership; the engine-tuning settings below are then ignored). |
| `busy_timeout_ms` | `5000` | SQLite only: how long a writer waits on a lock before erroring. |
| `pool_size` / `max_overflow` | `20` / `40` | Connection-pool sizing for the engine firm builds. |
| `default_queue` | `"default"` | Reserved for callers that want a shared default. |
| `preserve_finished_jobs` | `True` | Keep finished jobs (stamp `finished_at`) vs. delete on finish. See [Queues & retention](queues.md#finished-job-retention). |

`configure()` returns a `Runtime` and also installs it as the process-global, retrievable with
`firm.queue.current_runtime()`. The engine is created lazily on first use, so
`configure()` is cheap and fork-safe (a forked child calls `runtime.reset()` before its first query).

To reuse your app's existing engine (one pool for app + queue):

```python
import firm.queue as bq
from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg://localhost/myapp")
bq.configure(engine=engine)
```

## The engine & connection pool

`configure()` builds a SQLAlchemy engine tuned for firm's access pattern:

- **SQLite:** WAL journal mode, `busy_timeout`, foreign keys on, and `BEGIN IMMEDIATE` for claims.
- **Postgres/MySQL:** `pool_pre_ping` + `pool_recycle=3600` so idle/stale connections recover
  transparently.
- A generous pool (`pool_size=20`, `max_overflow=40`) so many worker threads + the dispatcher,
  scheduler, and heartbeat loops never starve.

You rarely need to touch these; `configure(pool_size=…, max_overflow=…)` adjusts the pool, and
`configure(engine=…)` bypasses firm's engine entirely.

## Where settings matter

- `preserve_finished_jobs` and retention → [Queues & retention](queues.md).
- Worker/dispatcher/scheduler tuning lives in the **supervisor** configs
  ([Workers & the supervisor](workers-and-supervisor.md)), not here.
- Database choice and drivers → [Database backends](../database-backends.md).
