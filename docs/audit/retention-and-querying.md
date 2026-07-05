# Retention & querying

## Querying with `history()`

```python
audit.history(
    subject=None,            # a domain object, ("Type", id), a Ref, or a bare "label" string
    subject_type=None,        # or filter by type alone …
    subject_id=None,           # … or id alone (either, or both, independently)
    actor=None,                  # same shape as subject
    actor_type=None,
    actor_id=None,
    action=None,                    # exact match
    correlation_id=None,
    since=None,                        # datetime — only events at/after this time
    limit=100,
)
```

A reference passed to `subject=`/`actor=` filters on its `(type, id)` — any display name is
ignored. A bare string is a type, so `actor="cron"` is exactly `actor_type="cron"`: it filters by
type only and matches every role-labelled `cron` event regardless of id.

Filters combine with AND. Results are newest-first (by `id`). Every filter hits an indexed
scalar column — `history()` never filters on `data`/`changes`/`context` (see
[Internals](internals.md)). Pass either the paired form (`subject=`) or the split form
(`subject_type=`/`subject_id=`) for a given field, never both — mixing them raises `ValueError`.

```python
audit.history(subject=invoice, limit=10)                  # this invoice's history
audit.history(actor=user, since=last_week)                 # what this user did recently
audit.history(action="invoice.paid", correlation_id=rid)   # one request's payment event
audit.history(subject_type="Invoice")                       # every invoice, any id
audit.history(actor_type="model")                             # everything a model actor did
audit.history(actor="cron")                                    # a role/label actor (type only)
```

The same-transaction `record(conn, ...)` path has no equivalent inline query — open your own
connection and call `audit.history(...)`, or query `firm.audit.schema.audits` directly if you
need something `history()` doesn't express.

## Retention

firm-audit keeps events **forever by default** (`max_age=None`) — an audit log that silently
drops history defeats its own purpose, so pruning is opt-in, not automatic.

```python
AuditLog(database_url="postgresql://localhost/myapp", max_age=7776000.0)   # prune after 90 days
```

Unlike `firm.cache`'s eviction, retention is **never triggered by writes** — `record()` never
calls into it, no matter how short `max_age` is. Pruning only happens when you ask for it:

```python
audit.retention.run_once()    # -> number of events deleted
```

or on a timer, if you opt in:

```python
AuditLog(
    database_url="postgresql://localhost/myapp",
    max_age=7776000.0,
    background_retention=True,
    retention_interval=3600.0,
)
```

or from the CLI — see [CLI](cli.md):

```bash
firm-audit prune --database-url sqlite:///audit.db --max-age 7776000
```

> **Note:** with `max_age` unset, the audit log never prunes — it grows until you explicitly set
> a retention policy. For compliance-sensitive deployments, this is the point: nothing deletes
> audit history unless you've configured it to.
