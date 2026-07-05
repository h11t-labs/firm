# Proposal: independent subject/actor filtering in `firm.audit`

*Filed from real usage in `mail-triage` — a consumer app that adopted `firm.audit` as its
domain audit log. Two gaps in `history()` (and the identical gap it inherits in `firm.ui`'s
audit dashboard) currently force the consumer to either drop a working filter capability or
maintain a parallel query layer against `firm.audit.schema.audits` directly.*

## Summary

`history()`'s `subject=` and `actor=` parameters only accept a full `(type, id)` pair — there is
no way to filter by type alone or id alone. `firm.audit`'s `actor` model also assumes every actor
is a referenceable entity with an id, which doesn't fit apps whose actors are plain role labels
(`"system"`, `"model"`, `"cron"`) with no natural id. Both gaps are reachable today only by
bypassing `history()` and querying `firm.audit.schema.audits` directly — which the docs already
point to as the escape hatch (`docs/audit/retention-and-querying.md:26-28`), so this isn't a new
need, just one worth closing in the library instead of every consumer reimplementing it.

## Motivating use case

mail-triage records one audit row per state transition (`action.decided`, `folder_move.completed`,
etc.) via a thin wrapper around `firm.audit.events.append`, joining the caller's transaction.
Its `/audit` page exposes **four independent, optional filter fields** — event type, subject id,
subject type, actor type — any of which can be used alone or combined:

- *"show me everything for this one message"* → subject id alone
- *"show me every audit row for a rule, any rule"* → subject type alone
- *"show me everything the model decided, across all messages"* → actor type alone
- *"show me everything a human did"* → actor type alone, again

The first is expressible today (`subject=("message", mid)`). The second and both actor-type
cases are not — there is no way to say "subject type X, any id" or "actor type Y, any id" through
`history()`.

## Current behavior

`firm/audit/events.py`:

```python
def history(
    conn, *, subject=None, actor=None, action=None, correlation_id=None, since=None, limit=100,
):
    stmt = select(_audits).order_by(_audits.c.id.desc()).limit(limit)
    if subject is not None:
        subject_type, subject_id = _ref(subject)
        stmt = stmt.where(_audits.c.subject_type == subject_type, _audits.c.subject_id == subject_id)
    if actor is not None:
        actor_type, actor_id = _ref(actor)
        stmt = stmt.where(_audits.c.actor_type == actor_type, _audits.c.actor_id == actor_id)
    ...
```

`_ref()` only accepts `None`, a `(type, id)` tuple, or an object with `.id` — there is no path to
"type only":

```python
def _ref(obj):
    if obj is None:
        return None, None
    if isinstance(obj, tuple):
        kind, ident = obj
        return kind, str(ident)
    if not hasattr(obj, "id"):
        raise TypeError(f'{type(obj).__name__} has no `.id`; pass an explicit ("Type", id) tuple instead.')
    return type(obj).__name__, str(obj.id)
```

The gap propagates unchanged into `firm.ui`'s audit dashboard. `firm/ui/audit_queries.py`
queries `schema.audits` directly (for pagination/sorting `history()` doesn't offer), but its
filter parsing has the identical shape:

```python
def _split_ref(value: str | None) -> tuple[str, str] | None:
    """Parse a ``Type:id`` search-box value into the ``(type, id)`` pair a filter needs."""
    if not value or ":" not in value:
        return None
    kind, _, ident = value.partition(":")
    return (kind, ident) if kind and ident else None
```

The dashboard's Subject and Actor search boxes are single text inputs labeled `"Subject
(Type:id)"` / `"Actor (Type:id)"` — both halves are required, or the filter is silently dropped
(`_split_ref` returns `None` on a bare type with no `:id`).

## Why this matters beyond mail-triage

Both columns are already indexed for partial lookups —
`Index("index_firm_audits_on_subject", "subject_type", "subject_id")` and the equivalent for
actor are composite indexes, so `WHERE subject_type = ?` alone is still a fully indexed prefix
scan, not a table scan. The schema already supports this query shape; only the query-building
code doesn't expose it.

The actor-as-entity assumption is the sharper mismatch. `docs/audit/overview.md`'s own example —
`actor=user` — assumes a domain object. But plenty of real actors in audit logs aren't domain
objects at all: background jobs, cron triggers, system defaults, model/LLM calls, IMAP/webhook
sync — anything with a "who did this" answer that isn't a user account. Right now those all have
to be shoehorned into a `(type, id)` pair with a fabricated or empty id, or dropped from
`actor=` filtering entirely and left to text search on `data`.

## Proposed changes

### 1. Independent `*_type`/`*_id` parameters on `history()`

```python
def history(
    conn,
    *,
    subject: Subject | tuple[str, Any] | None = None,
    subject_type: str | None = None,
    subject_id: Any | None = None,
    actor: Subject | tuple[str, Any] | None = None,
    actor_type: str | None = None,
    actor_id: Any | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if subject is not None and (subject_type is not None or subject_id is not None):
        raise ValueError("pass either subject= or subject_type=/subject_id=, not both")
    if actor is not None and (actor_type is not None or actor_id is not None):
        raise ValueError("pass either actor= or actor_type=/actor_id=, not both")

    if subject is not None:
        subject_type, subject_id = _ref(subject)
    if subject_type is not None:
        stmt = stmt.where(_audits.c.subject_type == subject_type)
    if subject_id is not None:
        stmt = stmt.where(_audits.c.subject_id == str(subject_id))
    # actor: same pattern
    ...
```

Fully backward compatible: `subject=`/`actor=` behave exactly as today when used alone; the new
parameters are purely additive, and mixing the paired and split forms for the same field is a
clear `ValueError` rather than silently picking one.

### 2. A bare-label shorthand for actors with no id

```python
def _ref(obj):
    if obj is None:
        return None, None
    if isinstance(obj, str):
        return obj, None                     # NEW — a role label, no id
    if isinstance(obj, tuple):
        kind, ident = obj
        return kind, str(ident)
    if not hasattr(obj, "id"):
        raise TypeError(...)
    return type(obj).__name__, str(obj.id)
```

So `record(conn, action="sync.completed", actor="system")` stores `actor_type="system",
actor_id=NULL` directly, instead of requiring `actor=("system", "")` or omitting the actor
entirely. This composes with (1): `history(actor_type="model")` then finds every row recorded
with a bare-string actor of that type, or an id-bearing one — no special-casing needed on the
read side, since both still write to the same two indexed columns.

I'd propose this only for `actor`, not `subject` — a "subject" is by definition the thing being
acted on, and in every real audit-log use case I can think of it has an identity even when the
*actor* doesn't. Happy to be argued out of that asymmetry if there's a use case for label-only
subjects too.

### 3. Cascade into `firm.ui`'s dashboard

Once (1) lands, `audit_queries.py`'s `_apply_filters`/`_split_ref` can delegate to the same
independent-column logic instead of parsing a combined string, and the filter form gets two
fields per row instead of one:

```html
<label>Subject type<input name="subject_type"></label>
<label>Subject id<input name="subject_id"></label>
<label>Actor type<input name="actor_type"></label>
<label>Actor id<input name="actor_id"></label>
```

This also sidesteps a minor fragility in the current `"Type:id"` text format: it only works
cleanly because `partition(":")` splits on the *first* colon, so it degrades ungracefully for any
future id format that legitimately wants a colon before the id portion. Independent fields have
no such ambiguity.

## Alternatives considered

- **Do nothing; keep pointing consumers at `schema.audits` directly.** This is the documented
  status quo and it works, but it means every consumer with this need (not just mail-triage —
  anything with more than one meaningfully-typed subject/actor) reimplements the same
  query-composition code the library already almost has. It also means `firm.ui`'s dashboard,
  the one first-party consumer, has the same gap with no workaround short of forking it.
- **A single `filters: dict` catch-all instead of named parameters.** Rejected — loses the
  typed, autocomplete-able signature `history()` currently has, for no real flexibility gain
  over explicit optional parameters.
- **Make `subject=`/`actor=` accept a 1-tuple or a bare string as "type only," instead of adding
  new parameters.** Considered, but `subject=("message",)` reads ambiguously next to
  `subject=("message", mid)`, and silently changing what a 1-element tuple means is a sharper
  edge than an additive parameter. The explicit `subject_type=`/`subject_id=` split is clearer at
  the call site and mirrors the column names directly.

## Compatibility

Purely additive at the `history()` level — no existing call sites change behavior. The `_ref()`
change for bare-string actors is additive too (strings weren't a valid `actor=`/`subject=` input
before; today they'd raise `TypeError` via the `hasattr(obj, "id")` branch). No schema or migration
changes — both columns and their composite indexes already exist.
