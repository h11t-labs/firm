# Getting started

## Install

```bash
pip install firm-audit              # or: uv add firm-audit
pip install "firm-audit[postgres]"     # psycopg, for PostgreSQL
pip install "firm-audit[mysql]"        # PyMySQL, for MySQL/MariaDB
```

See **[Installation](../installation.md)** for the full list of extras.

## Create an audit log

```python
from firm.audit import AuditLog

audit = AuditLog(database_url="sqlite:///audit.db")
```

By default `AuditLog(...)` creates the `firm_audits` table if it's missing
(`create_schema=True`). For production schema management, use the bundled Alembic migration and
pass `create_schema=False` — see [Database backends](../database-backends.md#migrations).

## Record events

```python
audit.record(
    "invoice.paid",
    subject=invoice,                  # a domain object with `.id`, or ("Invoice", invoice_id)
    actor=current_user,
    data={"amount": 4200, "currency": "USD"},
    correlation_id=request_id,
)

audit.history(action="invoice.paid")  # -> most recent events first
```

## References: the subject and the actor

`subject` and `actor` are **references**, and both accept the same forms — type and id are each
optional, so you're never forced to invent an id for a non-entity actor:

| You pass | Stored as `(type, id, name)` | When |
|---|---|---|
| a domain object with `.id` | `(ClassName, str(obj.id), None)` | the common case — a model instance |
| `("Invoice", 42)` | `("Invoice", "42", None)` | explicit type + id |
| `"cron"` (a bare string) | `("cron", None, None)` | a **role/kind** with no record: `system`, `cron`, a webhook, an LLM |
| `Ref("User", 7, name="alice@example.com")` | `("User", "7", "alice@example.com")` | attach a human-readable **display name** |
| `None` (the default) | `(None, None, None)` | a system event with no actor/subject |

```python
from firm.audit import Ref

audit.record("sync.ran", actor="cron")                        # a role, no id
audit.record("user.login", actor=Ref("User", 7, name=email))  # id + display name
audit.record("system.boot")                                   # no actor at all
```

The display **name** is stored so the row stays legible after the referenced record is deleted or
renamed — an audit log outlives the data it describes. It's display-only; filters never touch it. A
bare string becomes the *type* (a role you can filter on with `history(actor="cron")`), so pass a
string *identity* like an email as `("user", email)`, not bare. For a model whose audit identity
isn't `.id`, define `__firm_audit_ref__(self) -> Ref` on it and it will be used automatically.

Two more free-form channels ride alongside `data`: `changes` for a before/after diff (by convention
`{field: [before, after]}`, e.g. `changes={"status": ["pending", "paid"]}`) and `context` for
request metadata (`{"ip": ..., "request_id": ...}`).

## The same-transaction guarantee

The reason to reach for firm-audit instead of an external log: when your app and the audit log
share a database, pass the **connection already inside your transaction**, and the audit row
commits or rolls back together with the business change — never one without the other.

```python
from firm.audit import record

with engine.begin() as conn:
    mark_invoice_paid(conn, invoice_id)
    record(conn, "invoice.paid", subject=invoice, actor=current_user, data={"amount": 4200})
# both commit together, or both roll back — there is no in-between state
```

`AuditLog.record(..., conn=...)` does the same thing through the facade — pass `conn` to join an
existing transaction, or omit it to let `AuditLog` open and commit its own (a separate,
non-atomic write — see [Configuration](configuration.md#two-topologies)).

## Clean up

An `AuditLog` owns a connection pool (and, optionally, a background retention thread). Close it
when you're done, or use it as a context manager:

```python
with AuditLog(database_url="sqlite:///audit.db") as audit:
    audit.record("user.login", actor=user)
# closed automatically
```

## A complete example

```python
from firm.audit import AuditLog

with AuditLog(database_url="sqlite:///audit.db") as audit:
    audit.record("user.login", actor=("User", 7), context={"ip": "127.0.0.1"})
    audit.record(
        "invoice.paid", subject=("Invoice", 42), actor=("User", 7), data={"amount": 4200}
    )

    for event in audit.history(limit=10):
        print(event["created_at"], event["action"])
```

Next: **[Retention & querying](retention-and-querying.md)** and
**[Configuration](configuration.md)**.
