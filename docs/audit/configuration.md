# Configuration

Every `AuditLog(...)` option, with its default:

```python
AuditLog(
    database_url=None,              # SQLAlchemy URL (or pass engine=)
    engine=None,                    # a pre-built SQLAlchemy Engine, instead of database_url
    create_schema=True,             # create firm_audits if missing
    max_age=None,                   # prune events older than this many seconds; None = keep forever
    background_retention=False,     # run a retention loop on a timer
    retention_interval=3600.0,      # seconds between background retention runs
)
```

| Option | Default | Notes |
|---|---|---|
| `database_url` / `engine` | — | Provide one. `engine` lets the audit log share a pool with your application — see [Two topologies](#two-topologies). |
| `create_schema` | `True` | Set `False` if you manage the schema with Alembic. |
| `max_age` | `None` (keep forever) | Pruning is opt-in — see [Retention & querying](retention-and-querying.md). |
| `background_retention` / `retention_interval` | `False` / `3600.0` | Opt-in timer-based pruning. |
| `on_error` | traceback to stderr | Callback for background-pruning failures. |

Call `audit.close()` (or use the `with` form) to stop the background loop and dispose the engine.

## Two topologies

**Shared database** — pass `engine=` pointing at your application's own engine (or the same
`database_url`). `AuditLog.record(..., conn=...)` and the module-level `record(conn, ...)` then
write inside *your* transaction: atomic with the business change, the same-transaction guarantee.

```python
from firm.audit import AuditLog

audit = AuditLog(engine=app_engine)          # same database as the rest of the app
```

**Separate database** — point `database_url` at a dedicated audit database. Writes are durable
but **not atomic** with the business change (they're different databases — no single transaction
can span both). `record()` raises on failure; the caller decides what to do. A transactional
outbox (atomic append into a local table, relayed to the remote audit database) would close this
gap, but firm-audit doesn't build one — it's out of scope for now.

```python
audit = AuditLog(database_url="postgresql://audit-host/audit_db")  # independent durable write
```

Both topologies use the same `record()` / `history()` API — the only difference is whether you
pass `conn`.

## Sharing an engine

```python
from firm._core.database import create_engine_for

engine = create_engine_for("postgresql://localhost/myapp")
audit_a = AuditLog(engine=engine)
audit_b = AuditLog(engine=engine, create_schema=False)
```
