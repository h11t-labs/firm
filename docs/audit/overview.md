# firm-audit — overview

An append-only, database-backed audit log for Python. Record who did what, when, and to what — in
the same database (and, if you want, the same transaction) as the change itself — on SQLite,
PostgreSQL, or MySQL/MariaDB.

firm-audit is **not a port of a Rails Solid gem** — there is no `solid_audit`. It's an original
firm module that shares the same "you already have a database" thesis as `firm.cache` and
`firm.channel`. See [Comparison to Rails](../comparison-to-rails.md).

## Why an audit log in the database?

The reason to put an audit trail in the same database as the data it describes — rather than an
external log shipper or event store — is the **same-transaction guarantee**: write the audit row
inside the transaction that makes the change, and a recorded event can never exist without its
business change, or vice versa. That guarantee is only available when both live in one database;
nothing external can give it to you.

firm-audit also supports a **separate** audit database when you'd rather isolate it — durable, but
no longer atomic with the business write. See [Configuration](configuration.md).

## The model

One table, `firm_audits`:

| Column | Purpose |
|---|---|
| `id` | Autoincrement — total event order. |
| `action` | What happened, e.g. `"invoice.paid"`. |
| `subject_type` / `subject_id` / `subject_label` | Polymorphic target of the action — each part optional; `*_label` is a display name captured at event time. |
| `actor_type` / `actor_id` / `actor_label` | Polymorphic actor — who (or what) did it; same optional shape. |
| `correlation_id` | Groups events from one request/transaction. |
| `data` | Free-form JSON payload. |
| `changes` | Free-form JSON before/after diff (`{field: [before, after]}`). |
| `context` | Free-form JSON request metadata (ip, request id, ...). |
| `created_at` | When the event was recorded. |

Actor and subject are **references**: pass a domain object (`.id` → its id), an explicit
`("Type", id)` tuple, a bare `"label"` string (a role/kind like `"cron"` — stored as the type), or
a `Ref(type, id, name)` — and each of type / id / name is optional, so a non-entity actor needs no
invented id. See [Getting started](getting-started.md#references-the-subject-and-the-actor).

`data`/`changes`/`context` are stored as JSON **text**, not native JSON/JSONB — see
[Internals](internals.md) for why, and what it means for querying. `subject_label`/`actor_label`
are display-only and, like the JSON payloads, never filtered on in SQL.

## What it does

- `record` — append one event, optionally inside a caller-supplied transaction (the
  same-transaction path) — see [Getting started](getting-started.md).
- `history` — query events by subject / actor / action / correlation id / time — filter by a full
  subject/actor, or by type or id alone — see [Retention & querying](retention-and-querying.md).
- **Append-only by construction**: the public API has no update or delete. The only thing that
  ever removes a row is opt-in, age-based **retention** — off by default (keep forever) — see
  [Retention & querying](retention-and-querying.md).

```python
from firm.audit import AuditLog, record

# shared DB, atomic with a business change:
with engine.begin() as conn:
    mark_invoice_paid(conn, invoice_id)
    record(conn, "invoice.paid", subject=invoice, actor=user, data={"amount": 4200})

# standalone:
log = AuditLog(database_url="sqlite:///audit.db")
log.record("user.login", actor=user)
log.record("sync.ran", actor="cron")  # a non-entity actor — a role, no id
log.history(action="user.login")
```

Read on: **[Getting started](getting-started.md)**.
