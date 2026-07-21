# Internals

## Schema

One table, `firm_audit_events`:

```
id              autoincrement PK, total event order
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
```

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
