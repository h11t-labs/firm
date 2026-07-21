# Design: tamper-evident audit log (firm-audit)

Status: draft for review — not implemented.

## Goal

Make it practically impossible to alter the audit trail without detection, against an
attacker who has **full read/write access to the database** (SQL console, leaked
credentials, a rogue admin) but **not** the application's secret key. Concretely, detect:

- **modification** of an existing row,
- **deletion** of a row,
- **insertion** of a row from outside the application (or replaying a copy of a real row),
- **truncation** of the tail ("the last hour never happened"),
- dropping or resetting the whole table.

### Non-goals

- Protection against an attacker who holds both the database *and* the secret key *and*
  the external anchor. Tamper-*evidence*, not tamper-*proofing*; that is the ceiling for
  any software-only log (same trust model as AWS QLDB digests or certificate-transparency
  checkpoints).
- Preventing destruction. Someone with DB access can always `DROP TABLE`; the guarantee is
  that this cannot go unnoticed, not that it cannot happen.
- Confidentiality (rows stay plaintext).

### Constraints from the existing package

- **Opt-in.** Without a configured key, behavior and schema semantics stay exactly as
  today. No new required infrastructure (no Redis, no leader election service).
- **Append-only contract stays intact.** Nothing may `UPDATE` an audit row — including us.
  Whatever we add must be written at insert time or live in a separate table.
- **The same-transaction guarantee stays intact.** `record(conn=...)` joins the caller's
  transaction and may be held open for as long as that transaction runs.
- **Multi-writer is the norm.** Multiple app instances (and threads) insert concurrently
  over plain SQLAlchemy connections; works on SQLite, Postgres, and MySQL.

## Why not the obvious design (a per-row hash chain)

The textbook approach — every row stores `HMAC(key, prev_row_hash ‖ row)` — is **rejected**,
and the reason is exactly the multi-writer question:

1. **It serializes every writer.** Each insert needs the previous row's hash, so all
   writers across all app instances contend on one chain head (a `SELECT … FOR UPDATE` on
   a head row). Worse: with `record(conn=caller_conn)`, that lock is held until the
   *caller's* transaction commits. Two app transactions that both record an audit event
   would serialize end-to-end — a global throughput bottleneck and a deadlock factory.
2. **Rows commit out of order.** With concurrent writers, row id 42 can become visible
   before id 41 (whose transaction is still open). A chain that assumes "previous row" is
   meaningful at insert time fights the database's own concurrency model.
3. **Rollback gaps.** Postgres sequences burn ids on rollback, so id continuity can never
   be the integrity signal anyway.

Instead the design splits the problem into a **lock-free per-row layer** (protects content,
scales with any number of writers) and an **asynchronous sealing layer** (protects
existence/ordering, needs no coordination beyond a unique constraint).

## Design overview

```
Layer 1  row MAC       every row self-authenticates      catches: modify, forge, replay
Layer 2  seals         chained blocks over sealed rows   catches: delete, reorder, seal-tampering
Layer 3  anchor        chain head exported externally    catches: tail truncation, table drop
         verify        CLI + API that checks all three
```

```
 WRITE PATH (N instances, no coordination)      SEAL PATH (any instance, DB arbitrates)
 ┌────────────┐  ┌────────────┐                 ┌─────────────────────────────┐
 │ instance A │  │ instance B │   ...           │ SealLoop (every `interval`) │
 └─────┬──────┘  └─────┬──────┘                 └──────────────┬──────────────┘
       │ record()      │ record()                              │ hwm = to_id of last seal
       ▼               ▼                                       ▼
   ULID + row_mac = HMAC(key, canonical(row))    select id > hwm AND created_at <= now-grace
       │               │                         (batches of <= 10k, id order)
       ▼               ▼                                       │
 ┌──────────────────────────────────┐                          ▼
 │  firm_audit_events (append-only) │◄──── reads ────  rows_mac over (id, row_mac)…
 │  + entry_id UNIQUE, row_mac      │                          │
 └──────────────────────────────────┘                          ▼
                                                 ┌───────────────────────────┐
                                                 │ firm_audit_seals          │
                                                 │ seq UNIQUE ── race loser  │
                                                 │ prev_mac chain            │──► anchor sink
                                                 └───────────────────────────┘    (file / callback,
                                                                                   best-effort)
 VERIFY (read-only, anywhere the key lives)
 anchor ──► seal chain (seq dense, prev_mac) ──► per range: row_count + rows_mac
        ──► per row (keyset-paginated): recompute row_mac
 verdicts: OK · WARNING(late-commit / anchor-stale) · UNPROTECTED(legacy) · TAMPERED
```

### Layer 1 — per-row MAC (synchronous, zero contention)

Two new nullable columns on `firm_audit_events`:

| column     | type          | purpose                                                        |
|------------|---------------|----------------------------------------------------------------|
| `entry_id` | `String(26)`  | client-generated ULID, **unique index** — identity + anti-replay |
| `row_mac`  | `String(64)`  | hex `HMAC-SHA256(key, canonical(row))`                         |

At insert time (inside `events.append`, when a key is configured) the writer generates a
ULID, captures `created_at = now_utc()` **once** and feeds that same value to both the MAC
input and the insert (outside voice #4 — two `now_utc()` calls would differ by
microseconds and make every row verify as TAMPERED), and computes:

```
row_mac = HMAC(key, "v1" ‖ entry_id ‖ action ‖ subject_type ‖ subject_id ‖ subject_label
                    ‖ actor_type ‖ actor_id ‖ actor_label ‖ correlation_id
                    ‖ data ‖ changes ‖ context ‖ created_at_iso)
```

Canonicalization: fixed field order, length-prefixed fields (no escaping ambiguity), JSON
payloads exactly as stored (they are already serialized strings), absent values as a
distinguished marker. A `"v1"` version prefix allows evolving the recipe later.

**Round-trip rule (review 2A).** The MAC input is built from values *as the database will
return them*, not as they sit in memory: `created_at` normalized to naive-UTC ISO-8601 with
microseconds (matching `dt_type()` — timezone-naive, `DATETIME(6)` on MySQL), scalar refs
after `_norm()` coercion, JSON payloads as their stored `Text` strings. Anything the DB
round-trips lossily would otherwise verify as a false TAMPERED on one dialect and not
another — the classic failure mode of DIY implementations. A per-dialect property test
(insert → read back → recompute == stored MAC) on SQLite, Postgres, and MySQL is a
mandatory part of the test matrix.

Properties:

- **No coordination.** The MAC depends only on the row itself — inserts from N instances
  proceed exactly as today: same transaction semantics, no locks, no reads, no extra
  round-trips. This is the load-bearing decision for multi-writer support.
- **Modification** → recomputed MAC mismatches. Any column change, including `created_at`.
- **Out-of-band insertion** → no key, so no valid `row_mac`. NULL/garbage MACs are flagged.
- **Replay** (copying a real row to fabricate history) → duplicate `entry_id` is rejected
  by the unique index at insert; verify additionally reports any duplicates.
- Not covered: **deletion** — a removed row simply isn't there to check. That is Layer 2's job.
- Honest framing (outside voice #11): `row_mac` does not bind the DB `id` — in the
  *unsealed* tail a valid `(entry_id, row_mac)` pair is not yet pinned to its position;
  id↔content binding arrives with the seal. The docs describe the unsealed window as
  "modification/forgery-protected, position- and deletion-protected only once sealed".

### Layer 2 — seals (asynchronous chained blocks)

New table `firm_audit_seals`:

| column       | type         | purpose                                              |
|--------------|--------------|------------------------------------------------------|
| `seq`        | int, **unique** | dense seal counter (1, 2, 3, …)                   |
| `kind`       | string       | `"seal"` or `"checkpoint"` (see Retention)           |
| `from_id`    | bigint       | first audit id covered (exclusive of previous seal)  |
| `to_id`      | bigint       | last audit id covered                                 |
| `row_count`  | int          | number of rows present in `(from_id, to_id]` at seal time |
| `rows_mac`   | `String(64)` | `HMAC(key, concat of (id, row_mac) in id order)`     |
| `prev_mac`   | `String(64)` | `seal_mac` of seal `seq-1` (`"genesis"` for seq 1)   |
| `seal_mac`   | `String(64)` | `HMAC(key, seq ‖ kind ‖ from_id ‖ to_id ‖ row_count ‖ rows_mac ‖ prev_mac ‖ sealed_at)` |
| `sealed_at`  | datetime     | when sealed                                          |
| `key_id`     | `String(16)` | which key signed this (rotation, see below)          |

A background **`SealLoop`** (same `InterruptiblePoller` pattern as `RetentionLoop`, default
interval 60 s, opt-in via `AuditLog(..., background_sealing=True)` or `firm-audit seal`):

1. Read the high-water mark: `to_id` of the latest seal.
2. Select rows with `id > hwm` **and `created_at <= now − grace`** (grace default 60 s),
   ordered by id, **in batches of at most `seal_batch_size` (default 10 000) rows** — after
   sealer downtime the backlog becomes several successive seals, never one monster
   transaction (review 7A; same batching pattern as retention's `_BATCH_SIZE`).
3. If any: compute `rows_mac` over the rows actually present, insert the seal with
   `seq = last + 1` in one short transaction. Loop until the backlog is drained.
4. **NULL-MAC rows are sealed too** (review 5A): a row without `row_mac` is hashed with an
   explicit `"nomac"` marker, so its deletion is still detected. The very first seal
   records the **activation boundary** (the highest pre-existing row id); verify treats
   NULL-MAC rows at or below it as "unprotected (legacy)" and NULL-MAC rows above it as
   TAMPERED — a configured writer never produces them, so a missing MAC after activation
   is either config drift on one instance (fix the env var) or a forged insert. Either
   way it must surface loudly within one seal interval.
5. **Two-phase rollout** (outside voice #3): key presence and sealing are separate
   switches, because key-enablement is never atomic across instances. Phase 1: deploy
   `FIRM_AUDIT_KEY` everywhere — rows start carrying MACs, no boundary exists yet, and a
   straggler instance writing NULL-MAC rows is harmless. Phase 2: enable sealing
   (`background_sealing=True` / `firm-audit seal`) once all writers carry the key — the
   first seal records the boundary against a fleet that is already fully MAC-writing.
   This keeps 5A's TAMPERED semantics at full strength without a false-red on every
   rollout; the docs prescribe the order and the sealer's startup log restates it.

The **grace window** is how out-of-order commits are handled: a row's `created_at` is set
at insert, so any transaction still open when its rows would be sealed must be older than
`grace`. As long as `grace` exceeds the longest application transaction that records audit
events (plus clock skew between instances), every row is committed and visible before its
id range gets sealed. `grace` is configurable; the docs state this sizing rule explicitly
and `AuditLog` emits a startup hint when sealing is enabled (review 1A). A legitimate
transaction that still outruns `grace` lands in an already-sealed range: verify classifies
a row with a *valid* MAC in a sealed range as a **LATE-COMMIT WARNING**, never a TAMPERED
verdict — false alarms are what train people to ignore real ones. Only an invalid or
missing MAC in a sealed range is tampering.

No single `grace` fits heterogeneous transaction durations (outside voice #5): a
40-minute ETL either warns on every run or forces a fleet-wide 40-minute
deletion-unprotected tail. The resolution is a documented pattern, not a mechanism
(review D14): long-running jobs record via the existing own-transaction path (omit
`conn` — durable immediately, deliberately not atomic with the job) or record after
commit; `grace` stays tight (60 s) for everything else, and the LATE-COMMIT WARNING text
points at this pattern by name.

Properties:

- **Deletion of a sealed row** → `row_count` and `rows_mac` no longer match → detected.
- **Insertion into a sealed range** → extra row → `rows_mac`/count mismatch → detected.
  Verify distinguishes: extra row with a *valid* MAC is a LATE-COMMIT WARNING (tune
  `grace` up); anything else is tampering (see grace-window paragraph above).
- **Rollback id-gaps are harmless** — seals hash the rows actually present, never assume
  id continuity.
- **Seal tampering** (edit/delete/reorder a seal) → the `prev_mac` chain plus dense unique
  `seq` breaks → detected. Forging a seal requires the key.
- **Detection window**: a row is deletion-protected only once sealed, i.e. after at most
  `grace + seal interval` (default ≈ 2 minutes). Bounded, documented exposure; Layer 1
  still protects unsealed rows against modification and forgery.

#### Multiple writers and the sealer

- **Appends need nothing.** Layer 1 is per-row; N instances insert fully in parallel.
- **The sealer needs no election.** Every instance may run `SealLoop`; the unique
  constraint on `seq` (plus the hwm read) is the arbiter. If two sealers race, one insert
  wins and the other hits a unique violation, rolls back, and simply retries next tick —
  benign, portable to SQLite/MySQL/Postgres, no extra infrastructure. (Running it on one
  instance is merely a micro-optimization, not a correctness requirement.)
- **Clock skew** between instances only widens the needed `grace`, never corrupts the
  chain — `created_at` ordering is not an integrity input, id order is.

### Layer 3 — external anchor

The chain head `(seq, seal_mac)` is worthless if the attacker can also rewrite it — so it
must periodically leave the database:

- After each successful seal, the sealer appends `"<sealed_at> <seq> <seal_mac>"` to
  `FIRM_AUDIT_ANCHOR_PATH` (a local append-only file) if set, and/or invokes an
  `on_anchor` callback for custom sinks (ship to S3, a second database, a log pipeline, a
  Slack webhook — anything the DB attacker can't reach).
- Anchor writes are **best-effort** (review 3A): a failed write (disk full, path gone,
  callback exception) routes to `on_error` — never crashes or rolls back the seal, never
  fails silently. Verify reports the age of the newest anchor so a stalled sink is visible.
- `firm-audit verify --anchor <path>` checks that some seal matches the newest anchor and
  that the recorded chain extends it.

This closes the remaining holes: **tail truncation** (deleting the latest seals + rows) and
**wholesale reset** (drop/recreate an empty-but-valid chain) both contradict the anchor.

**What the anchor is worth depends on where it lives** (review 3A) — the docs carry this
table so nobody overestimates their protection:

| Deployment | Anchor | Guarantee |
|---|---|---|
| Anchor shipped off-host (S3, second DB, log pipeline) | remote | full Layer 3: truncation + reset detected |
| Anchor file on an app host, DB elsewhere | local file | protects against DB-only compromise |
| Everything on one host (typical SQLite setup) | local file | Layer 3 is nominal — whoever owns the host owns DB, key, and anchor. Layers 1+2 still stop the DB-credentials-only attacker |
| No anchor configured | none | Layers 1+2 only: modify/forge/delete of sealed rows detected; tail truncation is not |

### Verification

`firm-audit verify [--db …] [--anchor PATH] [--from-seq N] [--full]` and
`AuditLog.verify()`:

1. Recompute `row_mac` per row (Layer 1) — reading via **keyset pagination on id**
   (bounded memory, no long cursors; review 7A) — report rows with missing/invalid MACs.
2. Walk the seal chain: dense `seq`, `prev_mac` linkage, recompute `rows_mac`/`row_count`
   per range (Layer 2).
3. Compare against the anchor if given (Layer 3), including anchor age.
4. Report the unsealed tail (informational) and late-commit warnings.

**Rolling full coverage** (review D12, superseding plain incremental 7A): each run
verifies the unsealed tail and the newest seals *plus a rotating slice of older sealed
ranges*, sized so every range is recomputed at least once every `verify_cycle` runs
(default 7 — nightly cron ⇒ full coverage weekly). Plain tail-only incremental would
never re-read old ranges, so an edit in last week's range would stay invisible until a
manual `--full` — the headline guarantee must hold for the *default* run, with a bounded,
documented detection delay. Resume/rotation state is **advisory only**: it optimizes
scheduling but is never trusted for a verdict — the seal chain and anchor are the
authority, so an attacker rewriting the state file (outside voice #8) only reorders work,
never suppresses detection. A from-genesis `--full` pass is prescribed when verify state
and the DB share a host. Every finding carries one of four verdict classes (review 1A/5A):

| Verdict | Meaning |
|---|---|
| `OK` | chain, seals, rows, and anchor all consistent |
| `WARNING` | valid-MAC row in a sealed range (late commit — tune `grace` or use the long-job pattern), or unsealed tail older than a threshold (sealer liveness, review D15) |
| `UNPROTECTED` | NULL-MAC rows at or below the activation boundary (written before the key existed) |
| `TAMPERED` | invalid/missing MAC after activation, count/`rows_mac` mismatch, broken seal chain, anchor contradiction |

Exit code 0 = OK/UNPROTECTED only; non-zero = any TAMPERED (WARNINGs exit 0 but print) —
with one exception (review D16): when `--anchor` is given and the newest anchor is older
than `anchor_max_age` (default 3× the seal interval, configurable), verify exits
**non-zero**. The silently-truncatable window between the last anchored seal and the chain
head is the one thing only Layer 3 guards; letting it grow unboundedly behind an exit-0
warning would degrade the only guarantee the anchor exists to give (outside voice #7).
Anchor *writes* stay best-effort (3A) — strictness lives on the verification side.
Verification is read-only and can run anywhere the key is available.

### Key management

- Key from `FIRM_AUDIT_KEY` (or `AuditLog(..., mac_key=...)`); no key → feature off,
  columns stay NULL, everything behaves as today.
- **Hard validation at startup** (review 4A): the key is a UTF-8 string of at least 32
  characters — shorter is a hard error, not a warning (a weak key silently voids all three
  layers). Empty/absent = feature off with a clear log line. Keyring entries in
  `FIRM_AUDIT_KEYS="id1=secret,id2=secret"` split on the *first* `=`; commas inside keys
  are rejected at parse time with a pointed error. Writer and verifier parse with the same
  function — a parse divergence would masquerade as tampering.
- `key_id` (first 8 hex chars of `SHA-256(key)`) is stored on rows and seals. Rotation:
  configure `FIRM_AUDIT_KEYS="id1=old,id2=new"` for verify while new writes use the new
  key. Old rows stay verifiable; no re-signing (that would require UPDATE).
- Verify hard-fails on an unknown `key_id` (a forged row can't just invent one — it still
  needs a valid MAC under a known key).

### Retention integration

Pruning deletes old rows, which would read as tampering. Resolution — **checkpoint seals**:

1. `Retention.run_once` aligns its cutoff to a seal boundary: it only deletes rows in
   ranges fully covered by seals older than the cutoff (never partial ranges, never
   unsealed rows).
2. **Retention only prunes what verifies** (review finding #3, P2). Before deleting a
   fully-expired sealed range, `run_once` **re-verifies** it — recomputing every row's
   `row_mac` from its content *and* the range's `rows_mac`/`row_count` against the seal
   (`verify.range_is_intact`, the same recompute the verifier runs; the canonicalization is
   factored into one shared helper, never duplicated). A range that no longer verifies is
   **refused**: pruning **stops at the first refused range** (simpler and safer than skipping
   past it), the checkpoint never advances past it, the count lands on
   `Retention.last_refused_tampered`, and the refusal routes through `on_error`. Without this
   gate an attacker could edit an old sealed row with a plain `UPDATE` (no key needed — the
   per-row `row_mac` column is left untouched, so `rows_mac`, which hashes the stored MAC
   strings, still matches its seal) and wait for it to age past `max_age`; a naive prune would
   then delete the row and checkpoint over it, after which verify reports OK — the tampering
   laundered instead of surfaced. With the gate the evidence stays in place and every later run
   refuses again until an operator investigates. **Cost:** pre-prune verification re-reads
   (keyset-paginated) every row it is about to delete. The sealing-off path (no key, or no
   seals) is unaffected and byte-identical to before.
3. After deleting, it writes a `kind="checkpoint"` seal recording `pruned_through_id` (in
   `to_id`) and carrying the chain forward (`prev_mac` → `seal_mac` as usual).
4. Verify skips row-recomputation for ranges at or below the newest checkpoint but still
   validates the seal chain across it. Rows *above* the checkpoint that are missing remain
   violations. Old seals below the checkpoint may be pruned too.

The batched `FOR UPDATE SKIP LOCKED` delete loop stays; it just iterates whole sealed
ranges instead of raw `created_at` batches.

Sealed-only pruning gives retention a hidden dependency on sealer liveness (outside voice
#6): with a stalled sealer, nothing past the last seal is prunable and the table grows
despite `max_age`. That failure must be loud, not silent (review D15): `run_once` returns
and logs the count of expired-but-unsealed rows it had to skip, `firm-audit prune` prints
it, a skip count above a threshold routes through `on_error`, and verify's
unsealed-tail-age WARNING (verdict table above) independently flags the stalled sealer.

### Schema & migration

- Alembic migration `0002`: rename `firm_audits` → `firm_audit_events` (with its secondary
  indexes) to match the `firm_<module>_<entity>` convention, then add nullable `entry_id` +
  `row_mac` + `key_id`, unique index on `entry_id`, create `firm_audit_seals`. Nullable columns
  are zero-downtime; the unique index is **not** by default (outside voice #9) — on
  Postgres it is created with `CREATE UNIQUE INDEX CONCURRENTLY` (autocommit block in the
  migration), and the docs carry an explicit caveat that MySQL/SQLite take a blocking
  index build whose duration scales with table size (run it in a quiet window on large
  tables).
- MySQL deployments must use `utf8mb4` for the audit table (outside voice #10): a lossy
  charset silently mangles 4-byte characters between MAC-time and read-back — under
  tamper-evidence that surfaces as false TAMPERED instead of staying invisible. Docs
  requirement + the 2A property test injects adversarial inputs (emoji/4-byte unicode,
  NUL bytes, very long strings), not just ASCII.
- Pre-existing rows (from before the key was configured) verify as "unprotected" —
  reported once as a count, not as tampering. A future `--strict` flag can make them fail.
- New audit columns are not part of the dashboard read surface.

### Dashboard verify-status panel (in scope per review D11; spec per design review D22–D25)

`verify` persists its latest outcome in a single-row status table
`firm_audit_verify_status`: `ran_at`, `outcome` (`ok|warning|error|tampered`), per-class
verdict counts, `error_message`, `last_full_coverage_at` + cycle progress,
`newest_anchor_at` + `anchor_configured` (so "no anchor by design" is distinguishable from
"anchor stale"), unsealed-tail size and age, affected identifiers on tampering (seal seqs
+ id ranges), and run duration. The panel reads through the existing dashboard query layer.

**Form (approved board variant A): a state-adaptive strip above the KPI `.cards` row on
the audit page.** Integrity is categorically prior to event counts, so it renders above
them — but in the OK state it is one calm ~40 px line that recedes; in the TAMPERED state
it expands to a full-width banner that dominates. One fixed footprint cannot both recede
on the 999 green days and dominate on the one red day.

Component mapping (existing vocabulary only — no new hex values, no new CSS language):
badge = `pill.lg` with `.ok`/`.warn`/`.danger`; UNPROTECTED/legacy counts and
"not configured" use the *neutral* base `pill` / `Empty` (legacy is neither healthy nor
an alarm); timestamps via `When` (hover = absolute time, advances with the dashboard's
auto-refresh); banner detail rows via `Kv`; `role="alert"` on the TAMPERED banner. Every
state carries icon + word (✓ ⚠ ⛔ + "OK/WARNING/TAMPERED") — color never signals alone.

State table (what the operator *sees*):

| State | Rendering |
|---|---|
| OK | thin strip: `pill.ok` "✓ integrity OK" · secondary line "full coverage N ago (cycle k/N) · anchor N old · unsealed tail N rows" · `When` "verified N ago" |
| WARNING | strip with `--warn-bg`; the body **itemizes the cause** — "verify hasn't run in 26 h (expected ≤ verify_max_age)" vs "2 late commits" vs "anchor stale" are distinct texts, never one undifferentiated amber |
| ERROR (design D24) | amber strip carrying the failure itself: "verify failed: unknown key_id ab12… — check FIRM_AUDIT_KEYS"; counts toward liveness; red stays reserved for proven tampering |
| TAMPERED | full-width `--danger` banner: heading with finding count, `Kv` rows naming the affected seal seqs / id ranges as links into the audit table, "first detected" timestamp, and a next-step line ("preserve the DB unchanged · run `firm-audit verify --full` · docs") — red is never a dead end |
| Configured, never ran | amber "never verified — key + sealing active since N; schedule a `firm-audit verify` cron" (distinct from not-configured: disambiguated on key/sealing config, **not** on table emptiness) |
| Not configured | neutral `Empty`-style line "verification not configured — set FIRM_AUDIT_KEY…"; no alarm on no-key deployments |

Staleness: a `verify_max_age` config (analogous to `anchor_max_age`) forces the strip to
amber when `ran_at` exceeds it, regardless of the stored verdict — a verify cron that died
must surface within one threshold, not silently age. Green is honestly scoped: the strip
states last *full-coverage* age, not just last-run age, so "green" cannot mean "only the
tail was swept" without saying so.

**Escalation to the overview page (design D23):** TAMPERED and amber liveness states
render the same strip/banner at the top of the overview page too — the alarm may not hide
behind the audit tab. The OK strip stays audit-only, so the overview is unchanged on
green days.

**Mobile (375 px, design D25):** the strip wraps to two lines — line 1 badge + `When`
(the essence), line 2 the secondary detail line, truncatable with title-hover. The
TAMPERED banner stacks its `Kv` rows vertically with ≥44 px touch targets on links/CTAs.

Tests: status row upsert by verify (incl. error and affected-range fields); rendering per
state-table row (six states); overview escalation renders only on TAMPERED/amber-liveness;
staleness forcing (`verify_max_age`); anchor-absent-by-design shows neutral, never stale;
mobile wrap snapshot.

### Recommended deployment hardening (docs, not code)

Orthogonal, worth a docs section: give the app a DB role with `INSERT`+`SELECT` only on
`firm_audit_events` (no `UPDATE`/`DELETE`; a separate role for retention), so casual tampering
is *prevented*, with the MAC/seal layers catching whoever can bypass the grants. Not
portable to SQLite, hence docs-only.

## Alternatives considered

- **Synchronous per-row hash chain** — rejected: serializes all writers across all
  instances, holds the chain lock for entire caller transactions, deadlock-prone, breaks
  on out-of-order commits. See "Why not the obvious design".
- **Per-writer (per-instance) chains** — parallel writes, but verification must stitch N
  streams, every stream needs its own anchor, and deleting an *entire* stream is
  indistinguishable from an instance that never existed. Complexity without a matching
  payoff.
- **Plain SHA-256 chain without a key** — an attacker with DB access recomputes the whole
  chain after editing; only protects against accidental corruption. HMAC with an external
  key is what turns "consistent" into "authentic".
- **Database triggers / audit-by-DB** — engine-specific, unavailable on SQLite, and a DB
  admin (the threat model) can drop the trigger. Kept as optional hardening, not the
  mechanism.

## NOT in scope

- **Two-key split (writer vs seal key)** — deferred to `TODOS.md` (review D10): valuable
  hardening, but it changes the "any instance may seal" story; v2.
- **`--strict` verify flag** (legacy rows as failures) — only meaningful once
  installations with pre-key data exist.
- **DB-grant hardening as code** — docs-only: not portable to SQLite, and the DBA is the
  threat model.
- **Signed timestamps / RFC 3161 / transparency-log integration** — third-party
  provability is a different product; the anchor covers internal detection.

## Implementation plan (rough)

1. `integrity.py`: canonicalization, MAC helpers, keyring parsing + key validation (pure
   functions, heavy unit tests: embedded separators, unicode, None vs `""`, `=` inside key
   values, comma rejection, `< 32` chars → hard error).
2. Schema + migration 0002 (incl. an upgrade test against a populated 0001 database).
3. `events.append`: ULID + `row_mac` when key configured.
4. `sealing.py`: `Sealer.run_once` (batched, activation boundary, NULL-MAC marker) +
   `SealLoop` + anchor sink.
5. `verify` (API + CLI, verdict classes, incremental + keyset pagination).
6. Retention alignment + checkpoint seals.
7. Docs: `docs/audit/tamper-evidence.md` (threat model + deployment/anchor table, grace
   sizing rule, key rotation, DB-grant hardening).
8. firm-ui: `firm_audit_verify_status` table + dashboard status panel (review D11).

### Test matrix (review 6A — each item is a named test, written with its feature)

**CRITICAL regression:** without `FIRM_AUDIT_KEY`, behavior and schema semantics are
byte-identical to today (existing suite green + explicit no-key assertions).

- *Canonicalization:* per-dialect round-trip property test (SQLite/Postgres/MySQL):
  insert → read back → recompute == stored MAC (review 2A).
- *Tamper matrix:* edit of each individual column; delete sealed row; forged insert;
  replay (duplicate `entry_id`); count-preserving swap (delete sealed row + forged
  replacement, same count); truncate seal tail; reorder seals; edit a seal; delete a
  mid-chain seal; drop-and-recreate table. Each → TAMPERED.
- *Verdict classes:* late commit beyond grace → WARNING not TAMPERED (1A); NULL-MAC row
  after activation → TAMPERED, at/below activation → UNPROTECTED (5A); unknown `key_id`;
  stale/missing anchor; empty log; single-row log; legacy-only log.
- *Sealer:* two concurrent sealers race on `seq` (loser retries benignly); crash mid-seal →
  idempotent resume from hwm; rollback id-gap inside range → OK; backlog > batch size →
  multiple seals; anchor write failure → `on_error` fired, seal still committed (3A).
- *Retention:* aligned prune + checkpoint seal; verify across a checkpoint; prune refuses
  unsealed rows; retention with sealing off unchanged.
- *CLI:* verify exit codes (0 / non-zero), `firm-audit seal`, prune output.
- *E2E lifecycle* (real Postgres and MySQL in CI): N parallel writers + background sealer
  + prune with checkpoint + `verify --anchor` → OK end-to-end.
- *Dashboard:* the test list in "Dashboard verify-status panel" above (six state-table
  rows, overview escalation, `verify_max_age` forcing, anchor-absent-by-design, mobile
  wrap) — reviews D11 + D22–D25.
- *Outside-voice hardening (D12–D17):* edit in an old sealed range is detected by the
  default run within `verify_cycle` runs; a corrupted/attacker-written verify state file
  cannot suppress detection (state is advisory); two-phase rollout scenario — straggler
  writer without key during phase 1 produces no TAMPERED; `--anchor` with anchor older
  than `anchor_max_age` → non-zero exit; prune with stalled sealer reports the
  skipped-unsealed count and verify flags unsealed-tail age; `created_at` captured once
  (MAC input == stored value, unit-level); canonicalization property test includes emoji,
  NUL bytes, and very long strings.

## Approved Mockups

| Screen/Section | Mockup | Direction | Notes |
|----------------|--------|-----------|-------|
| Audit page — verify-status panel | design board `verify-panel-design-board.html` (session scratchpad; variants A/B/C, states per variant) | **Variant A** — state-adaptive strip above KPI cards; OK recedes to one line, TAMPERED expands to banner | firm-ui tokens only; UNPROTECTED/not-configured neutral; icon+word in every state |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Voice | independent subagent (Codex CLI unavailable: platform outage) | Independent 2nd opinion | 1 | CLEAR (PLAN) | 11 findings; 6 decision points resolved (D12–D17, all A), 4 folded as hardening, 1 premise note (settled by D2) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 7 issues, 0 critical gaps, all resolved (1A 2A 3A 4A 5A 6A 7A) |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR (FULL) | score 5/10 → 9/10; 11 subagent findings folded; decisions D22–D25 (variant A, ERROR state, overview escalation, mobile wrap); Codex design voice timed out → single-model |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** eng outside voice confirmed the canonicalization risk class (2A) and
  extended it (created_at capture, charset); its challenges to 7A/5A/3A were accepted as
  refinements (rolling verify coverage D12, two-phase rollout D13, strict anchor age D16).
  Design review ran single-model (Codex timeout); its critical find: the spec mapped four
  verdict classes onto three colors — UNPROTECTED now renders neutral.
- **VERDICT:** ENG + OUTSIDE VOICE + DESIGN CLEARED — ready to implement. Scope: full
  design (D2=A) + dashboard verify-status panel (D11=C, spec D22–D25); two-key split
  deferred to TODOS.md (D10=A).

NO UNRESOLVED DECISIONS
