# Internals

## Storage adapters

`AuditLog` is the single audit implementation. `events.build_values` normalizes references,
serializes typed payloads once, fixes the timestamp and signs the exact stored bytes. Both
instance writes and the standalone SQL `record(conn, ...)` function use it. Adapters do not
receive signing keys or implement signing, sealing, verification or retention policy.

The typed `Storage` contract covers persistence (`record`, idempotent `write`, `get`, `history`,
import provenance), maintenance scopes (`snapshot`, `claim`, `connect`), verify status,
archive acknowledgements and lifetime. Scopes yield a `StorageView`: row and seal reads,
seal insertion, archive membership, deletion and atomic-or-recoverable floor advancement.
`SqlStorage` enforces these scopes with transactions and row locks; `S3Storage` uses a lease
and a durable prune intent. The verifier asks only for a storage snapshot.

An adapter's `requires_entry_id` indicates whether unsigned writes also need a client identity.
SQL preserves its historical unsigned NULL metadata; S3 uses the identity for retry claims.
`stream` identifies the storage's native event namespace for archiving and outbox delivery;
SQL uses `None` because its archive stream is supplied separately. These are persistence
requirements, not alternate audit policy.

To support another storage, implement both contracts and run the shared policy scenarios against
it. Tests exercise signing, history, sealing, verification and archive-before-prune through a
wrapper that hides concrete adapter attributes. Storage-specific transaction, concurrency and
crash-recovery behaviour still needs its own adapter tests. Schema tables remain SQL's supported
read surface; the CLI and dashboard select storage without selecting another audit class.

## Source organization

The source follows the storage seam:

```text
firm/audit/
  log.py, events.py, prepared.py        shared event preparation and public AuditLog
  integrity.py, sealing.py, verify.py   shared signing and verification policy
  retention.py                         shared pruning policy
  storage/
    base.py                            Storage and StorageView contracts
    sql/
      store.py                         SQL persistence and transaction scopes
      projections.py                   bounded SQL verification reads
      archive.py                       SQL archive ledger and restore
    s3/
      store.py                         S3 objects, indexes and maintenance leases
      queries.py                       S3 dashboard reads
      outbox.py                        optional transactional SQL-to-S3 delivery
  archive/
    store.py                           verified batch publication and archive reads
    archiver.py                        archive-before-prune for either backend
  object_store.py, event_format.py      shared object client and wire format
  schema.py, queries.py, migrations/    stable SQL schema and read surfaces
```

`from firm.audit import AuditLog, SqlStorage, S3Storage` remains the public entry point.
Archival policy lives in `firm.audit.archive.Archiver`; SQL ledger details stay with the SQL
backend. The documented `s3_queries`, `s3_outbox` and `sql_archive` module paths forward to
these implementations for compatibility. `firm.audit.s3.S3AuditLog` also remains a thin
constructor over `AuditLog` and `S3Storage`. New storage adapters belong under `storage/`;
they reuse the event, integrity, maintenance and archival policy above that seam.

## Schema

The event table, `firm_audit_events`:

```
id              autoincrement PK, total event order (strictly monotonic; SQLite AUTOINCREMENT)
action          VARCHAR(255), not null — what happened
subject_type    VARCHAR(255) — polymorphic target type
subject_id      VARCHAR(255) — polymorphic target id
subject_label   VARCHAR(255) — display name captured at event time (display-only)
actor_type      VARCHAR(255) — polymorphic actor type
actor_id        VARCHAR(255) — polymorphic actor id
actor_label     VARCHAR(255) — display name captured at event time (display-only)
correlation_id  VARCHAR(255) — groups events from one request/transaction
data            TEXT — JSON-encoded free-form payload
changes         TEXT — JSON-encoded before/after diff
context         TEXT — JSON-encoded request metadata
created_at      timestamp — when the event was recorded
entry_id        VARCHAR(26) — client ULID, unique; identity + anti-replay (tamper-evidence)
row_mac         VARCHAR(64) — hex HMAC over the canonical row (tamper-evidence)
key_id          VARCHAR(16) — which key signed the row, for rotation (tamper-evidence)
```

The last three columns are **always present but nullable**. Ordinary SQL writes leave them NULL
until a key is configured. Explicit `prepare()` / `write()` retries also use an `entry_id`
without a key, for idempotency; `row_mac` and `key_id` remain NULL in that case. Enabling tamper-evidence adds no
columns — it only starts populating them, and starts writing the two side tables `firm_audit_seals`
(independent range seals plus the signed activation and floor markers) and
`firm_audit_verify_status` (the single row the verifier upserts, read by the dashboard). Those side
tables are detailed in [Tamper-evidence › Schema and migration](tamper-evidence.md#schema-and-migration).

Every part of a reference is nullable: an actor/subject may have a type without an id (a role
label like `cron`), an id without a type, or neither (a system event) — see
[Getting started](getting-started.md#references-the-subject-and-the-actor).

Indexes: `(subject_type, subject_id)`, `(actor_type, actor_id)`, `action`, `correlation_id`,
`created_at` — every column `history()` filters on. `subject_label`/`actor_label` are display-only
(they preserve a human name after the referenced record is gone) and, like
`data`/`changes`/`context`, are **not** indexed or queryable in SQL; see below.

## Append-only, by construction

`firm.audit.events.append` is the only function in the package that writes a row, and it only
ever issues an `INSERT`. `firm.audit.record` and `AuditLog.record` both funnel through it. There
is no `update`, and the only `delete` anywhere in the package is opt-in, age-based retention (see
[Retention & querying](retention-and-querying.md)) — recording never triggers it.

This is an application-level guarantee, not a database constraint: nothing stops raw SQL against
`firm_audit_events` from outside firm-audit. The guarantee is "the module is the sole writer, and it
only inserts."

## JSON-as-text, not JSONB

`data`/`changes`/`context` are stored as `Text` holding a JSON string — the same approach
`firm.queue` uses for job arguments — rather than native `JSON`/`JSONB`. This keeps the column
dialect-uniform (SQLite has no native JSON type) at the cost of SQL-side queryability: you can't
filter or index into the payload from SQL. In practice this is rarely a real loss —
`history()` filters on the indexed scalar columns (`action`, `subject`, `actor`,
`correlation_id`, `created_at`), and those are the fields an audit search actually needs. If you
need to query *inside* payloads routinely, consider also recording the relevant value as its own
scalar field, or query the table directly with your dialect's JSON functions on `data` after
parsing it yourself.

A small tagged-object protocol round-trips `datetime`/`date`/`Decimal`/`UUID` values inside
`data`/`changes`/`context`, independent of `firm.queue.serialization`.

## Same-transaction guarantee — the fine print

`record(conn, ...)` writes on exactly the connection you pass. The atomicity guarantee holds
only when `firm_audit_events` lives in the same database that connection belongs to — pass a
connection from a different database and the row is simply written there, untethered from
whatever transaction you think it's joining. There's no cheap way to detect this misuse at
runtime, so it's a documented contract rather than an enforced one.

## Portability types

The `id` PK uses the `Integer` variant on SQLite (so it maps to `INTEGER PRIMARY KEY`/rowid and
autoincrements), `BigInteger` elsewhere. `created_at` is `DATETIME(6)` on MySQL for sub-second
precision, matching `firm.cache` and `firm.queue`.
