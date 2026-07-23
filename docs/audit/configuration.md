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
    grace=60.0,                     # seal only rows older than this (settling window)
    seal_batch_size=10_000,         # max rows sealed per transaction
    anchor_path=None,               # append seal/floor/activation events here
    on_anchor=None,                 # callback(kind, from_id, to_id, mac, at) for custom sinks
    verify_cycle=7,                 # old-range verification cost divisor, not a day period
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
picture. Without a key, runtime behavior is inert; the migrated schema still contains the nullable
evidence columns and side tables.

| Option | Default | Notes |
|---|---|---|
| `mac_key` | `None` (env `FIRM_AUDIT_KEY`) | Feature key (the **row key**). Must be a UTF-8 string of **≥ 32 chars** — shorter is a hard error at startup. Pass `""` to force the feature off and ignore the environment. |
| `seal_key` | `None` (env `FIRM_AUDIT_SEAL_KEY`) | Optional **separate seal key** — signs independent seals, the activation marker, and retirement floors; row MACs keep using `mac_key`. Unset (or equal to `mac_key`) = single-key mode, unchanged. Same ≥ 32-char validation. Set it on **sealer/verifier hosts only** to shrink the blast radius of an instance compromise — see the [two-key split](tamper-evidence.md#two-key-split-a-separate-seal-key-optional-hardening). |
| `background_sealing` / `seal_interval` | `False` / `60.0` | Opt-in timer-based sealing (Layer 2), signing with the seal key. Enable only after the key is deployed fleet-wide — see the [two-phase rollout](tamper-evidence.md#rolling-it-out-key-first-then-sealing). |
| `grace` | `60.0` | Seals cover only rows older than this. The default `record()` path commits in its own short transaction; if you pass `conn=`, this window **must exceed that caller transaction plus clock skew** — see the [sizing rule](tamper-evidence.md#sizing-the-grace-window). |
| `seal_batch_size` | `10_000` | Max rows sealed per transaction, so a sealer backlog becomes several seals, never one monster transaction. |
| `anchor_path` | `None` (env `FIRM_AUDIT_ANCHOR_PATH`) | Append-only Layer-3 source for `SEAL`, `FLOOR`, `ACTIVATION`, and compacted `CHECKPOINT` lines. Mandatory for the full deletion/truncation guarantee; see placement below. |
| `on_anchor` | `None` | Callback `(kind, from_id, to_id, mac, at)` for shipping events off-host. Seal failures route to `on_error` and maximum coverage heals later; a floor sink failure refuses the prune. For verification, materialize callback-only history and pass it as `anchor_path`; the callback is a write sink, not a readable Layer-3 source. |
| `verify_cycle` | `7` | Cost divisor: default verify checks `ceil(range_count / verify_cycle)` date-selected ranges and always includes the newest range. It is not a period; the conservative rotation bound is `range_count` days. Only `full=True` / `--full` guarantees complete coverage. |

Rotation uses two role-scoped archives of **retired** keys, `FIRM_AUDIT_RETIRED_KEYS` (retired
**row** keys) and `FIRM_AUDIT_RETIRED_SEAL_KEYS` (retired **seal** keys), each
`"label=secret,label2=secret2"` — see [Key rotation](tamper-evidence.md#rotation).

### Anchor placement

Hard requirement: **DB compromise must not grant anchor write.** Choose the boundary appropriate
to the deployment:

1. A local app-host file is the weakest option. It is acceptable with a managed remote DB when
   compromised DB credentials cannot reach the app filesystem; it is risky on an all-in-one host.
2. A separate host or append-only mount is better because the DB attacker is outside that
   filesystem.
3. S3 Object Lock / WORM is strongest because storage enforces immutability.

Mutable files can be rotated with `firm-audit anchor-compact`, which replaces old history with one
signed coverage/floor `CHECKPOINT`. Do not compact WORM objects; rotate objects and expire older
ones through lifecycle policy. Without any anchor, a total wipe of hidden events, seals, and
verify status is undetectable.

### Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `FIRM_AUDIT_KEY` | writer, sealer, verify | The tamper-evidence secret — the **row key** (≥ 32 chars). |
| `FIRM_AUDIT_SEAL_KEY` | sealer, retention, verify | Optional **seal key** (≥ 32 chars). Signs seals, activation, and floors; unset = use the row key (single-key mode). Put it on sealer/verifier hosts only — see the [two-key split](tamper-evidence.md#two-key-split-a-separate-seal-key-optional-hardening). |
| `FIRM_AUDIT_RETIRED_KEYS` | verify, retention | Retired **row** keys (rotation): `"id1=old,…"`. Eligible for row-MAC validation only — **never** a seal. |
| `FIRM_AUDIT_RETIRED_SEAL_KEYS` | sealer heal, retention, verify | Retired **seal** keys: `"id1=old,…"`. Eligible for Layer-2 validation only. A single-key deployment puts its old key in **both** retired archives. |
| `FIRM_AUDIT_ANCHOR_PATH` | sealer, verify | Local anchor file path. |
| `FIRM_AUDIT_DATABASE_URL` | CLI | Default `--database-url` for `firm-audit` — see [CLI](cli.md). |

Call `audit.close()` (or use the `with` form) to stop the background loop and dispose the engine.

## Two topologies

**Shared database** — pass `engine=` pointing at your application's own engine (or the same
`database_url`). By default `AuditLog.record(...)` writes durably in its own short transaction.
`AuditLog.record(..., conn=...)` and the module-level `record(conn, ...)` instead write inside
*your* transaction: atomic with the business change. With sealing enabled, that caller transaction
must commit inside `grace`; a row that appears inside an already sealed range is tampering.

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
