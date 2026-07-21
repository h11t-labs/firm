# Configuration

Every `AuditLog(...)` option, with its default:

```python
AuditLog(
    database_url=None,              # SQLAlchemy URL (or pass engine=)
    engine=None,                    # a pre-built SQLAlchemy Engine, instead of database_url
    create_schema=True,             # create firm_audit_events if missing
    max_age=None,                   # prune events older than this many seconds; None = keep forever
    background_retention=False,     # run a retention loop on a timer
    retention_interval=3600.0,      # seconds between background retention runs
    mac_key=None,                   # tamper-evidence secret; None = feature off (env: FIRM_AUDIT_KEY)
    seal_key=None,                  # separate seal-side secret; None = use mac_key (env: FIRM_AUDIT_SEAL_KEY)
    background_sealing=False,       # run the seal loop on a timer (requires a key)
    seal_interval=60.0,             # seconds between seal runs
    grace=60.0,                     # seal only rows older than this (out-of-order commit window)
    seal_batch_size=10_000,         # max rows sealed per transaction
    anchor_path=None,               # append the seal-chain head here (env: FIRM_AUDIT_ANCHOR_PATH)
    on_anchor=None,                 # callback(seq, seal_mac, sealed_at) for custom anchor sinks
)
```

| Option | Default | Notes |
|---|---|---|
| `database_url` / `engine` | — | Provide one. `engine` lets the audit log share a pool with your application — see [Two topologies](#two-topologies). |
| `create_schema` | `True` | Set `False` if you manage the schema with Alembic. |
| `max_age` | `None` (keep forever) | Pruning is opt-in — see [Retention & querying](retention-and-querying.md). |
| `background_retention` / `retention_interval` | `False` / `3600.0` | Opt-in timer-based pruning. |
| `on_error` | traceback to stderr | Callback for background-pruning, sealing, and anchor-write failures. |

## Tamper-evidence

All opt-in and off until a key is set — see [Tamper-evidence](tamper-evidence.md) for the full
picture. Without a key, none of these have any effect and the schema behaves exactly as before.

| Option | Default | Notes |
|---|---|---|
| `mac_key` | `None` (env `FIRM_AUDIT_KEY`) | Feature key (the **row key**). Must be a UTF-8 string of **≥ 32 chars** — shorter is a hard error at startup. Pass `""` to force the feature off and ignore the environment. |
| `seal_key` | `None` (env `FIRM_AUDIT_SEAL_KEY`) | Optional **separate seal key** — signs `rows_mac`/`seal_mac` (seals + checkpoints); row MACs keep using `mac_key`. Unset (or equal to `mac_key`) = single-key mode, unchanged. Same ≥ 32-char validation. Set it on **sealer/verifier hosts only** to shrink the blast radius of an instance compromise — see the [two-key split](tamper-evidence.md#two-key-split-a-separate-seal-key-optional-hardening). |
| `background_sealing` / `seal_interval` | `False` / `60.0` | Opt-in timer-based sealing (Layer 2), signing with the seal key. Enable only after the key is deployed fleet-wide — see the [two-phase rollout](tamper-evidence.md#rolling-it-out-key-first-then-sealing). |
| `grace` | `60.0` | Seals cover only rows older than this. **Must exceed the longest audit-recording transaction plus clock skew** — see the [sizing rule](tamper-evidence.md#sizing-the-grace-window). |
| `seal_batch_size` | `10_000` | Max rows sealed per transaction, so a sealer backlog becomes several seals, never one monster transaction. |
| `anchor_path` | `None` (env `FIRM_AUDIT_ANCHOR_PATH`) | Local append-only file the seal-chain head is written to (Layer 3). |
| `on_anchor` | `None` | Callback `(seq, seal_mac, sealed_at)` for shipping the anchor off-host (S3, a second DB, a webhook). Failures route to `on_error`, never crash the seal. |

Rotation uses a separate **`FIRM_AUDIT_KEYS`** env var (`"label=secret,label2=secret2"`) read by
verify only — see [Key rotation](tamper-evidence.md#rotation).

### Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `FIRM_AUDIT_KEY` | writer, sealer, verify | The tamper-evidence secret — the **row key** (≥ 32 chars). |
| `FIRM_AUDIT_SEAL_KEY` | sealer, retention, verify | Optional **seal key** (≥ 32 chars). Signs seals + checkpoints; unset = use the row key (single-key mode). Put it on sealer/verifier hosts only — see the [two-key split](tamper-evidence.md#two-key-split-a-separate-seal-key-optional-hardening). |
| `FIRM_AUDIT_KEYS` | verify | Labelled keyring for rotation: `"id1=old,id2=new"`. |
| `FIRM_AUDIT_ANCHOR_PATH` | sealer, verify | Local anchor file path. |
| `FIRM_AUDIT_DATABASE_URL` | CLI | Default `--database-url` for `firm-audit` — see [CLI](cli.md). |

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
