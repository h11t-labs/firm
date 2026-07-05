# Implementation plan: independent subject/actor filtering in `firm.audit`

> **Status (superseded):** Phase A shipped. Phase B (label refs) shipped **generalized** — the
> reference model now accepts a bare `"label"` string, an explicit `("Type", id)` tuple with either
> half optional, a `Ref(type, id, name)` with an optional display **name**, and a
> `__firm_audit_ref__()` protocol, **symmetrically for both subject and actor** (not actor-only, as
> the "open question 3" below proposed). Display names are stored in first-class
> `subject_label`/`actor_label` columns. The null-id render fixes this doc insisted on were made in
> the CLI and the dashboard. Label refs are queried by type only (`history(actor="cron")`). See
> [getting-started.md](getting-started.md#references-the-subject-and-the-actor).

Assessment of, and plan for, the proposal *"independent subject/actor filtering in `firm.audit`."*
Verified against the current tree on `main`.

## Verdict

**Approve change 1 (independent `*_type`/`*_id` params), approve change 3 (UI cascade) with a
larger scope than the proposal claims, and split change 2 (bare-string actor) into a separate,
optional decision.** The core request is legitimate and cheap: the columns and composite indexes
already exist (`schema.py:45-46`), the read path only fails to *expose* the query shape, and the
docs already sanction the `schema.audits` escape hatch — so this is closing a gap, not inventing a
need. My reservations are about scope honesty, not about whether to do it.

Three things the proposal gets wrong or glosses over, in order of severity:

1. **The UI cascade is not "once (1) lands, delegate."** The clickable subject/actor *cells* and
   the detail page build a combined `Type:id` string and feed it back through the `subject=`/
   `actor=` query param (`render.py:1016-1032`, `render.py:1082-1087`). If change 3 replaces those
   params with split `subject_type`/`subject_id`, every click-to-filter link and every bookmarked
   `/audit?subject=Type:id` URL breaks unless we keep the combined param as an accepted alias. The
   proposal's HTML snippet only covers the *form*, not the links that populate it.

2. **Change 2 has a latent rendering bug the proposal never mentions.** The UI formats subject/
   actor as `f"{r['subject_type']}:{r['subject_id']}"` (`render.py:1017,1026,1082,1087`). A
   bare-string actor stores `actor_id = NULL`, which Python renders as the literal string
   `model:None` in the table, the chip, and the detail page. Change 2 cannot ship without fixing
   this, and the proposal budgets nothing for it.

3. **"Purely additive / fully backward compatible" is true for `history()` and false for the UI.**
   The `history()` signature change is genuinely additive. But renaming the dashboard's query
   params is a URL-contract change. Treat the two layers separately.

## What is solid and should ship as-is

- **Change 1 — independent params on `history()`** (`events.py:91-118`). Correct design. The
  `ValueError` guard against mixing `subject=` with `subject_type=`/`subject_id=` is the right call
  (better than silently picking one). The `str(subject_id)` coercion must match `_ref`'s existing
  coercion so `history(subject_type="Invoice", subject_id=1)` and `history(subject=("Invoice", 1))`
  return identical rows — the proposal's snippet does this correctly.
- **Rejecting the `filters: dict` and 1-tuple alternatives.** Both correctly rejected. The explicit
  split mirrors the column names and keeps the typed signature.

## Recommended scope

### Phase A — `history()` independent params (change 1)

Ship first, independently. Low risk, high value, no UI dependency.

- `events.py history()`: add `subject_type`, `subject_id`, `actor_type`, `actor_id` params; add the
  two `ValueError` guards; replace the two paired `.where(...)` blocks with the four independent
  ones (each column filtered only when its param is non-`None`). Coerce ids with `str(...)` exactly
  as `_ref` does.
- `log.py`: `AuditLog.history` (`log.py:116-135`) must forward all four new params — the proposal's
  snippet only edits `events.py` and forgets the class method that every consumer actually calls.
- **Semantic edge to decide and document:** `subject_id=None` currently means "no filter." A
  consumer that legitimately wants to match rows where `subject_id IS NULL` cannot express it
  through this design. That's acceptable (it matches today's behavior) but should be a one-line
  note in the docstring so nobody files it as a bug later.

### Phase B — bare-string actor (change 2) — OPTIONAL, decide explicitly

This is a data-model change (an actor with a type but no id) wearing a convenience-API costume.
It's defensible — `actor_id` is already nullable, and `actor=None` already produces a NULL id — but
it introduces two-ways-to-say-the-same-thing:

- `actor="system"` → `actor_type="system", actor_id=NULL`
- `actor=("system", "")` → `actor_type="system", actor_id=""`

A **type-only** read filter (`actor_type="system"`) unifies both, which is the intended read path
and works. But an exact-pair filter (`actor=("system", "")`) will *not* match the bare-string rows,
and vice versa. If Phase B ships, the docs must state that label actors should be queried by
`actor_type=` only, never by pair.

If Phase B ships, it **must** include the render fixes from Phase C step "NULL-id display" — they
are not optional, they are the same change. Do not merge B without them.

Recommendation: ship Phase A + C first; take Phase B only if mail-triage (or another consumer)
actually needs the write-side ergonomics rather than just writing `actor=("system", "")` themselves.
The read-side win (filter by actor type) is delivered entirely by Phase A and does not require B.

### Phase C — UI dashboard (change 3)

Larger than the proposal's snippet. Concretely:

1. **Split form fields.** `render.py audit_page` `_field` calls (`render.py:986-989`): replace the
   two combined inputs with four (`subject_type`, `subject_id`, `actor_type`, `actor_id`). Keep
   `action` and `correlation_id`.
2. **Query-param plumbing.** `server.py` `_audit` dispatch (`server.py:179-188`) reads
   `params.get("subject")`/`params.get("actor")`; `_audit` signature (`server.py:329-339`) and the
   two `audit_queries` calls (`server.py:352-375`) pass them through. All of this must be widened to
   the four split params, and the `filters` dict handed to `render.audit_page`
   (`server.py:363-368`) must carry all four keys so `_audit_href` round-trips them.
3. **`audit_queries.py`.** Replace `_split_ref` (`audit_queries.py:43-48`) and rework
   `_apply_filters`/`audit_count`/`audit_search` (`audit_queries.py:65-132`) to take the four split
   params and filter each column independently — the same logic as Phase A. Ideally factor the
   column-filter logic so the UI and `events.history` don't drift.
4. **Click-to-filter links + chips (the part the proposal omits).** `render.py:1016-1032` builds
   `subject_value = f"{type}:{id}"` and links via `{"subject": subject_value}`. Rewrite to emit
   `{"subject_type": type, "subject_id": id}` (and same for actor). The detail-page links
   (`render.py:1082-1087`) too.
5. **NULL-id display (shared with Phase B).** Guard the `f"{type}:{id}"` formatting so a NULL id
   renders as just the type (e.g. `system`), not `system:None`. Needed the moment any NULL-id row
   can exist — which is Phase B, but also any row already written with `actor=("x","")` renders
   `x:` today, so cleaning this up is worthwhile regardless.
6. **Backward-compat alias (recommended).** Keep accepting the old combined `subject=`/`actor=`
   query params in `_audit`, mapping `Type:id` → the split pair via the retained `_split_ref`, so
   old bookmarks and any external links keep working. Without this, change 3 is a silent
   URL-contract break — contradicting the proposal's "purely additive" framing.

## Tests (per the port test-parity requirement)

`tests/audit/test_history.py` currently covers only the paired forms. Add:

- `history(subject_type=...)` alone; `history(subject_id=...)` alone; both together equals the
  paired form.
- Same four cases for actor.
- `history(subject=(...), subject_type=...)` raises `ValueError`; same for actor.
- Parity: `history(subject_type="Invoice", subject_id=1)` == `history(subject=("Invoice", "1"))`
  (id coercion).
- If Phase B: `record(actor="system")` stores `actor_type="system", actor_id=None`; a bare string
  as `subject=` still raises `TypeError` (proposal restricts labels to actor only); `_ref("x")`
  unit test.
- UI: `tests/ui/test_server.py` — the split params filter correctly; the combined-param alias still
  works (if kept); a NULL-id actor row renders without `:None`.

## Docs to update

- `docs/audit/retention-and-querying.md:21-28` — the `history()` examples and the escape-hatch note
  (this filtering is now *in* the library).
- `docs/audit/overview.md` — the `actor=user` example, and (if Phase B) a bare-string-actor example
  plus the "query label actors by `actor_type=` only" caveat.
- `docs/audit/internals.md` — mentions `Type:id`; update if the UI param format changes.

## Open questions for the requester

1. Does mail-triage need the **write-side** `actor="system"` shorthand (Phase B), or only the
   **read-side** type filter (Phase A)? Phase A alone unblocks all four of the motivating filter
   cases.
2. Are there external/bookmarked `/audit?subject=Type:id` URLs to preserve? Determines whether the
   combined-param alias (Phase C step 6) is required or merely nice.
3. Confirm the asymmetry: label-only **actors** yes, label-only **subjects** no. The proposal
   proposes this and I agree — but it means `_ref` grows a string branch that behaves differently
   depending on caller (allowed for `actor`, rejected for `subject`), which `_ref` itself can't
   enforce since it doesn't know which field it's coercing. Enforce at the `append`/`history` call
   sites, not inside `_ref`, or accept that a bare-string subject silently works.
