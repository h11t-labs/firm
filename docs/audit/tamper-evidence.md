# Tamper-evidence

An audit log is only as trustworthy as its worst day — the day someone with database access
edits a row, deletes an inconvenient event, or drops the table and swears it never happened.
firm-audit's tamper-evidence layer is **opt-in** and makes those changes *detectable*: not
prevented (anyone with `DELETE` can delete), but impossible to do without leaving a mark that
verification will find.

It is off until you configure a key. **Without `FIRM_AUDIT_KEY`, nothing on this page is in
effect** — the schema, the write path, and every existing behaviour stay byte-for-byte what they
were. Turning it on adds columns and a side table; it never changes how un-keyed rows are read or
written.

## Threat model

The attacker has **full read/write access to the database** — a SQL console, leaked credentials,
a rogue DBA — but **not** the application's secret key. Against that attacker, verification
detects:

- **modification** of an existing row (any column, including `created_at`),
- **deletion** of a row,
- **insertion** of a row from outside the application, or **replay** of a copied real row,
- **truncation** of the tail ("the last hour never happened"),
- **dropping or resetting** the whole table.

This is tamper-*evidence*, not tamper-*proofing* — the same trust model as AWS QLDB digests or
certificate-transparency checkpoints. It is **not** designed to stop an attacker who holds the
database *and* the secret key *and* the external anchor, and it does not prevent destruction:
someone with access can always `DROP TABLE`; the guarantee is that this cannot go **unnoticed**.
Rows stay plaintext — this is integrity, not confidentiality.

### What the anchor is worth depends on where it lives

Detection of tail-truncation and wholesale reset is the anchor's job (Layer 3, below), and the
anchor is only as strong as its distance from the attacker. Read this table before you trust it:

| Deployment | Anchor | Guarantee |
|---|---|---|
| Anchor shipped off-host (S3, second DB, log pipeline) | remote | full Layer 3 — truncation + reset detected |
| Anchor file on an app host, DB elsewhere | local file | protects against a DB-only compromise |
| Everything on one host (typical SQLite setup) | local file | Layer 3 is nominal — whoever owns the host owns DB, key, *and* anchor. Layers 1+2 still stop the DB-credentials-only attacker |
| No anchor configured | none | Layers 1+2 only — modify / forge / delete of sealed rows detected; tail truncation is not |

## The three layers

Protection is split so that the fast path (writing rows) stays lock-free and multi-writer, and the
slow path (proving ordering) needs no coordination.

| Layer | Mechanism | Catches |
|---|---|---|
| **1 — row MAC** | every row self-authenticates with `HMAC-SHA256(key, canonical(row))` | modify, forge, replay |
| **2 — seals** | a background loop chains blocks over ranges of sealed rows | delete, reorder, seal-tampering |
| **3 — anchor** | the seal-chain head is exported outside the database | tail truncation, table drop |

**Layer 1 — per-row MAC.** At insert time, when a key is configured, the writer generates a ULID
(`entry_id`, unique-indexed — identity and anti-replay) and stores `row_mac`, a keyed HMAC over the
row's canonical bytes. It depends on **nothing but the row itself**: no locks, no reads, no extra
round-trips, so N app instances insert exactly as they do today. Editing any column breaks its MAC;
an out-of-band insert has no valid MAC; a replayed row collides on `entry_id`. In the *unsealed*
tail a valid `(entry_id, row_mac)` pair is modification- and forgery-protected but not yet pinned
to its position — id-to-content binding arrives with the seal.

**Layer 2 — seals.** A background `SealLoop` periodically hashes a contiguous range of settled
rows into a `firm_audit_seals` row: `rows_mac` over `(id, row_mac)` in id order, a dense unique
`seq`, and a `prev_mac` chain back to genesis. Deleting or inserting a sealed row breaks
`row_count`/`rows_mac`; editing or reordering a seal breaks the chain. Any instance may run the
loop — the unique `seq` constraint arbitrates races, so no leader election is needed.

**Layer 3 — anchor.** After each seal, the chain head leaves the database — appended to a local
file and/or handed to a callback (ship it to S3, a second database, a Slack webhook). Verification
checks that a seal matches the newest anchor and that the recorded chain extends it. This is what
closes tail-truncation and table-reset, which are otherwise invisible from inside the database.

## Rolling it out — key first, then sealing

Key presence and sealing are **two separate switches, enabled in order**, because enabling a key
is never atomic across a fleet.

1. **Phase 1 — deploy the key everywhere.** Set `FIRM_AUDIT_KEY` on every instance. Rows start
   carrying MACs. No seal boundary exists yet, so a straggler instance still writing MAC-less rows
   is harmless — it is not yet an alarm.
2. **Phase 2 — enable sealing**, once every writer carries the key: `background_sealing=True` (or
   run `firm-audit seal`). The **first seal records the activation boundary** — the highest
   pre-existing row id. From then on, a row *above* the boundary with no MAC is `TAMPERED` (a
   configured writer never produces one), while rows at or below it are the legacy
   `UNPROTECTED` set.

Doing it in the other order would make every rollout flash red while stragglers catch up. The
sealer restates this order in its startup log.

> Pre-existing rows, written before the key was configured, verify as **UNPROTECTED** — reported
> once as a count, never as tampering.

## Sizing the grace window

Seals only cover rows older than a **grace window** (`grace`, default 60 s). This is how
out-of-order commits are handled: a row's `created_at` is stamped at insert, so any transaction
still open when its range would be sealed must be younger than `grace`. **The rule: `grace` must
exceed the longest application transaction that records an audit event, plus clock skew between
instances.** As long as it does, every row is committed and visible before its id range is sealed.
`AuditLog` emits a startup hint restating this when sealing is enabled.

A legitimate transaction that still outruns `grace` lands in an already-sealed range. Verify calls
that a **`WARNING` (late commit)**, never `TAMPERED` — a valid MAC in a sealed range is a
latecomer, not an attack. Only an *invalid or missing* MAC in a sealed range is tampering. False
alarms are what train people to ignore real ones.

### Long jobs: record on their own transaction

No single `grace` fits both a 50 ms web request and a 40-minute ETL. Rather than widen `grace`
fleet-wide (which forces a long deletion-unprotected tail on everyone), **long-running jobs record
outside their own transaction**:

```python
from firm.audit import AuditLog

audit = AuditLog(engine=app_engine, mac_key="...")

# In a long job: record on the audit log's OWN connection (omit conn=), so the row is
# durable and settled immediately — deliberately not atomic with the 40-minute job.
audit.record("etl.completed", subject=batch, actor="etl", data={"rows": 1_000_000})
```

Recording via the own-transaction path (omit `conn=`) makes the row durable immediately, or record
*after* the job's transaction commits. `grace` then stays tight (60 s) for everything else, and the
late-commit `WARNING` text points at this pattern by name.

## Key management

The key comes from `FIRM_AUDIT_KEY` (or `AuditLog(..., mac_key=...)`). No key means the feature is
off: columns stay NULL and everything behaves as it did before tamper-evidence existed.

- **The key must be a UTF-8 string of at least 32 characters.** A shorter key is a **hard error at
  startup**, not a warning — a weak key silently voids all three layers, so it fails loudly
  instead. Empty or absent means the feature is simply off, logged as one clear line.
- `key_id` — the first 8 hex chars of `SHA-256(key)` — is stored on every row and seal so a
  verifier knows which key to check under. It is not the key and reveals nothing about it. Two
  *distinct* configured keys that happen to share a `key_id` (astronomically unlikely, but it would
  otherwise let one key shadow the other in the key_id-indexed keyring and flag the shadowed key's
  objects as a false `TAMPERED`) is a **hard error** — at startup for the row/seal pair, at verify
  time for a colliding retired key. Change one of the keys so their `key_id`s differ.

### Field length limits (signed path)

Under a key, over-length values fail **loudly at `record()` time** rather than being silently
truncated later. A non-strict MySQL truncates a value longer than its column — a `VARCHAR(255)`
scalar (`action`, the subject/actor type/id/label, `correlation_id`) or a `TEXT` JSON payload
(`data`/`changes`/`context`, max 65535 bytes) — which would leave the *stored* bytes different from
the *signed* bytes and make an untouched row verify as `TAMPERED`. So a value that would be clipped
raises a `ValueError` before the insert (put large detail in `data=`, and keep it under the TEXT
limit). Without a key the write path is unchanged — the database handles length exactly as it did
before tamper-evidence existed.

```python
# Explicit key (overrides the environment). Pass "" to force the feature off.
audit = AuditLog(engine=app_engine, mac_key="a-32-char-or-longer-secret-here!!")
```

### Rotation

Rotate without re-signing — re-signing would require an `UPDATE`, which the append-only contract
forbids. New writes use the new key; the **retired** key stays on the verifier so its old objects
still verify. Retired keys live in two **verify-only, role-scoped** archives — read only by
`firm-audit verify`, never by a writer or sealer:

- **`FIRM_AUDIT_RETIRED_KEYS`** — retired **row** keys. Eligible to validate row MACs only, and
  **never** a seal, in any mode.
- **`FIRM_AUDIT_RETIRED_SEAL_KEYS`** — retired **seal** keys. Eligible to validate seals *and* rows
  (a seal key is the higher-privilege key). A single-key deployment retires its one key here,
  because that key signed both its rows and its seals.

The archives never hold the *new* key — writers pick that up from `FIRM_AUDIT_KEY` /
`FIRM_AUDIT_SEAL_KEY` alone. Where the **old** key goes depends on what it signed:

| Deployment | Key you rotate | New key → | Retire the old key into | Its old objects that still verify |
|---|---|---|---|---|
| Single-key | the one key | `FIRM_AUDIT_KEY` (every writer) | `FIRM_AUDIT_RETIRED_SEAL_KEYS` | its rows **and** seals |
| Two-key split | row key | `FIRM_AUDIT_KEY` (every instance) | `FIRM_AUDIT_RETIRED_KEYS` | its rows (seals untouched) |
| Two-key split | seal key | `FIRM_AUDIT_SEAL_KEY` (sealer/verifier hosts) | `FIRM_AUDIT_RETIRED_SEAL_KEYS` | its seals (rows untouched) |

```bash
# Split deployment, rotating the ROW key. Instances carry the new row key:
export FIRM_AUDIT_KEY="the-new-32-char-or-longer-row-secret!!!!"

# The verifier holds the new keys plus the retired row key, labelled — split on the FIRST '='
# (a comma is always an entry delimiter; never put one in a secret):
export FIRM_AUDIT_SEAL_KEY="the-unchanged-32-char-or-longer-seal-key"
export FIRM_AUDIT_RETIRED_KEYS="2025=the-old-32-char-or-longer-row-secret"
firm-audit verify --anchor /var/lib/firm/audit.anchor
```

**Why the row archive is role-scoped.** A row key lives on every app instance, so an attacker who
compromises one holds it. If a rotated-out row key were still eligible to validate *seals*, that
attacker could wait for the rotation, then re-sign a sealed range under the stolen key and relabel
its `key_id` — laundering a rewrite of sealed history. `FIRM_AUDIT_RETIRED_KEYS` is therefore
row-only in **every** mode: a retired row key validates the rows it signed and nothing more. A seal
signed by a key that is not a *seal* key (current or retired) is unverifiable — a hard failure, never
a laundered OK.

Both archives split each entry on the **first** `=`, so a secret may itself contain `=`. A comma is
**always an entry delimiter** — a secret cannot contain one: `id1=A,id2=B` is two keys and is
byte-identical to a lone key whose secret were `A,id2=B`, so the two cannot be distinguished. A
comma that yields a malformed fragment (no `=`, empty label, or a too-short secret — the common
accidental case) is rejected with a pointed error; a comma followed by a well-formed `label=secret`
is taken as a separate key. Either way parsing is **fail-closed** — it never silently merges two
distinct secrets into one identity, and a genuine `key_id` collision between the parsed keys is a
hard error. **Do not put a comma in a secret;** use a longer comma-free random value. Writer and
verifier parse secrets with the same function — a parse divergence would masquerade as tampering.
Verify **hard-fails on an unknown `key_id`**: a forged row cannot invent a `key_id`, because it
still needs a valid MAC under a known key.

> **A leaked key is not a retired key.** The archives are for keys aged out on schedule, still
> trusted. A key that was **leaked or compromised** does **not** belong in any archive: adding it
> tells verify to trust MACs the attacker can now forge. Every row and seal it signed is no longer
> trustworthy — treat those objects as suspect and lean on the seals and anchors made under keys the
> attacker never held. Rotate the compromised key out of `FIRM_AUDIT_KEY` / `FIRM_AUDIT_SEAL_KEY`
> immediately, and do not archive it.

### Two-key split: a separate seal key (optional hardening)

By default one key signs everything, and it lives on **every** app instance — so compromising any
instance leaks the ability to forge not just rows but *seals*, and thus to rewrite sealed history
undetected. The two-key split shrinks that blast radius:

- Set a distinct **seal key** — `FIRM_AUDIT_SEAL_KEY` (or `AuditLog(..., seal_key=...)`) — on the
  **designated sealer/verifier hosts only**. It signs everything on the seal side (`rows_mac` and
  `seal_mac`, seals *and* checkpoints); ordinary app instances keep only `FIRM_AUDIT_KEY` and sign
  just their row MACs.
- After the split, an attacker who compromises an app instance holds only the row key and can forge
  at most an individual **unsealed** row — the seal chain is out of reach. Even editing a sealed
  row and recomputing its `row_mac` *and* the seal's `rows_mac`/`seal_mac` under the row key is
  caught: verify checks seals under the seal key, and refuses the row key as a seal signer.

```bash
# App instances (row MACs only):
export FIRM_AUDIT_KEY="the-32-char-or-longer-row-secret-value!!"

# Sealer / verifier hosts also carry the seal key:
export FIRM_AUDIT_SEAL_KEY="a-different-32-char-or-longer-seal-secret"
```

The verifier needs **both** keys (rows are checked under the row key, seals under the seal key);
configure `mac_key`/`FIRM_AUDIT_KEY` *and* `seal_key`/`FIRM_AUDIT_SEAL_KEY` on it. If verify meets a
seal signed by a key it holds only as a *row* key — the current row key, or a retired one from
`FIRM_AUDIT_RETIRED_KEYS` — it hard-fails and names both possible causes: *a two-key verifier
missing its seal key (add `FIRM_AUDIT_SEAL_KEY` / `FIRM_AUDIT_RETIRED_SEAL_KEYS`), or a
current-or-retired row key used to forge the seal (tampering)*.

**This is opt-in and the default is unchanged.** Leave `FIRM_AUDIT_SEAL_KEY` unset (or set it equal
to `FIRM_AUDIT_KEY`) and the seal key *is* the row key: every instance may seal, one key signs
everything, and behavior is byte-identical to single-key mode.

**The tradeoff is honest:** the split turns "any instance may seal" into a **designated sealer
role**. Sealing and retention's checkpoint-writing both need the seal key, so in a split deployment
they run on a sealer-role host, not just anywhere. A host without the seal key that tries to seal is
the usual loud no-op; a host without it that tries to prune the aligned path **refuses loudly**
(see [Retention and checkpoints](#retention-and-checkpoints)) rather than checkpoint with the wrong
key. You gain a smaller blast radius; you pay with a second secret to manage and a role to place.

## Verifying

`firm-audit verify` (and `AuditLog.verify()`) recompute Layer 1 per row (keyset-paginated on `id`
for bounded memory), walk the Layer 2 seal chain, and compare against the anchor for Layer 3. Every
finding carries one of four **verdict classes**:

| Verdict | Meaning |
|---|---|
| `OK` | chain, seals, rows, and anchor all consistent |
| `WARNING` | a valid-MAC row in a sealed range (late commit — tune `grace` or use the long-job pattern), or an unsealed tail older than a threshold (sealer liveness) |
| `UNPROTECTED` | NULL-MAC rows at or below the activation boundary — written before the key existed |
| `TAMPERED` | invalid/missing MAC after activation, `row_count`/`rows_mac` mismatch, a broken seal chain, or an anchor contradiction |

### Exit codes

- **Exit 0** — `OK` or `UNPROTECTED` only. `WARNING`s also exit 0, but they print.
- **Non-zero** — any `TAMPERED` finding.
- **Non-zero (anchor exception)** — when `--anchor` is given and the newest anchor is older than
  `anchor_max_age` (default: 3× the seal interval, configurable). The silently-truncatable window
  between the last anchored seal and the chain head is the one thing only Layer 3 guards; letting
  it grow unbounded behind an exit-0 warning would quietly degrade the only guarantee the anchor
  exists to give. Anchor *writes* stay best-effort — strictness lives on the verification side.

Verification is read-only and runs anywhere the key is available.

### Rolling full coverage

Re-reading every sealed range on every run is expensive; only ever reading the tail would let an
edit in last week's data hide until someone remembers to run `--full`. The default run threads the
needle: it verifies the **unsealed tail and the newest seals, plus a rotating slice of older sealed
ranges**, sized so every range is recomputed at least once every `verify_cycle` runs (default 7 —
a nightly cron gives full coverage weekly). For an **honest operator** an edit in an old range is
therefore caught by the *default* run within a bounded, documented delay.

The rotation state is **advisory only**: it schedules work but is never trusted for a verdict — the
seal chain, the anchor, the pruned-region-empty probe, and the unsealed tail are all walked in full
*every* run regardless. But the cursor is **not** MAC-protected, so it is not a coverage guarantee
against a *cursor-tampering* attacker: one who can write the state file can **pin** it — rewriting
it to the same value before every non-`--full` run — and keep one chosen older range out of the
rolling slice indefinitely, deferring recomputation of an edit there. **Only a periodic `--full`
guarantees every sealed range is recomputed** (it ignores the cursor); treat the rolling slice as an
accelerator for an honest operator, not proof that an old range was recently checked. Run a
from-genesis `--full` on a schedule — especially when the verify state and the database share a
host — and heed the warning verify emits whenever a non-`--full` run does a partial slice.

> **Green is honestly scoped.** The dashboard's integrity strip states the age of the last
> *full-coverage* pass, not just the last run — "green" never silently means "only the tail was
> swept".

## Retention and checkpoints

Pruning deletes old rows, which would read as tampering — so retention and sealing are wired
together with **checkpoint seals** (see [Retention & querying](retention-and-querying.md)):

1. `Retention.run_once` aligns its cutoff to a seal boundary — it deletes only rows in ranges
   **fully covered by seals older than the cutoff**, never partial ranges and never unsealed rows.
2. **Retention refuses to prune what verify would call `TAMPERED`.** Before deleting a
   fully-expired sealed range, it classifies the range with the **same classifier the verifier
   runs** — recomputing every row's `row_mac` from its content *and* the range's
   `rows_mac`/`row_count` against the seal (one classifier, two callers, so retention and verify can
   never disagree). A `TAMPERED` range — a deletion, a count-preserving swap, an invalid/missing
   MAC, an unverifiable seal — is **refused**: pruning stops there, the checkpoint never advances
   past it, the count lands on `Retention.last_refused_tampered`, and the refusal routes through
   `on_error`. This closes a laundering hole — an attacker who edits an old sealed row with a plain
   `UPDATE` (no key needed) and waits for it to age past `max_age` cannot get a naive prune to
   delete the evidence and checkpoint over it. The refusal stops at the first bad range and repeats
   every run until an operator investigates (`firm-audit verify --full`), so the evidence is
   preserved, never erased.

   A range whose only divergence is a **valid-MAC late commit** (a `WARNING`, not `TAMPERED` — see
   [Sizing the grace window](#sizing-the-grace-window)) is **not** refused. A transaction that
   outran `grace` and landed a genuine, validly-signed row in an already-sealed range is a latecomer,
   not an attack — and on real-concurrency backends (Postgres, MySQL) a writer racing the sealer's
   grace window makes this happen for real. Refusing it would block pruning forever over a benign
   event; instead the range is pruned (the late row is expired too, so deleting it with the range
   destroys no evidence). Only an *invalid or missing* MAC in a sealed range stops the prune. Trade-off:
   pre-prune classification **re-reads (keyset-paginated) every row it is about to delete** — a
   bounded read cost paid once per prune.
3. After deleting, it writes a `kind="checkpoint"` seal recording `pruned_through_id` and carries
   the chain forward as usual.
4. Verify skips row-recomputation at or below the newest checkpoint but still validates the seal
   chain across it. Rows *above* the checkpoint that go missing remain violations. It also asserts
   the pruned region is **empty**: retention deleted every row through the floor, so *any* surviving
   row at an id at or below the floor is a forged insert into a range the checkpoint records as
   holding zero rows — `TAMPERED`, caught by a bounded probe on *every* run (not just `--full`),
   never invisible just because verify skips recomputation there.

This gives retention a hidden dependency on **sealer liveness**: with a stalled sealer, nothing
past the last seal is prunable, so the table can grow past `max_age`. That failure is **loud, not
silent** — `run_once` returns and logs the count of expired-but-unsealed rows it had to skip,
`firm-audit prune` prints it, a skip count above a threshold routes through `on_error`, and
verify's unsealed-tail-age `WARNING` independently flags the stalled sealer.

A checkpoint is a seal, so in a [two-key deployment](#two-key-split-a-separate-seal-key-optional-hardening)
**retention needs the seal key**. Run pruning on a sealer-role host that has `FIRM_AUDIT_SEAL_KEY`.
On a host without it — one carrying only the row key — the aligned path would sign the checkpoint
with the wrong key, so `run_once` **refuses the whole aligned prune**: it deletes nothing, sets
`Retention.last_refused_no_seal_key`, routes the refusal through `on_error`, and `firm-audit prune`
prints it. (In single-key mode the seal key *is* the row key, so this never triggers and pruning is
unchanged.)

## Deployment hardening

The [two-key split](#two-key-split-a-separate-seal-key-optional-hardening) above is itself a
deployment-hardening measure — keeping the seal key off ordinary app instances — and composes with
the database grants below.

### Least-privilege database grants (docs-only)

Orthogonal to the MAC/seal layers, and worth doing where your database supports it: give the
application a role with **`INSERT` + `SELECT` only** on `firm_audit_events` — no `UPDATE`, no
`DELETE` — and a *separate* role for retention. Casual tampering is then *prevented* outright, and
the MAC/seal layers catch whoever can bypass the grants.

```sql
-- PostgreSQL sketch. The app role appends and reads but cannot mutate history.
CREATE ROLE firm_app LOGIN PASSWORD '...';
GRANT INSERT, SELECT ON firm_audit_events TO firm_app;
GRANT USAGE, SELECT ON SEQUENCE firm_audit_events_id_seq TO firm_app;

-- A separate role that retention (pruning) runs as:
CREATE ROLE firm_retention LOGIN PASSWORD '...';
GRANT SELECT, DELETE ON firm_audit_events TO firm_retention;
GRANT INSERT, SELECT ON firm_audit_seals TO firm_retention;  -- checkpoint seals
```

> A database first created on firm-audit 0.1.0 and upgraded through migration `0002` keeps its
> original Postgres sequence name `firm_audits_id_seq` (renaming a table does not rename its owned
> sequence). Use that name in the `USAGE, SELECT ON SEQUENCE …` grant on such a database; a
> freshly created schema uses `firm_audit_events_id_seq`.

This is deliberately **not** built into firm-audit as code: it is not portable to SQLite, and the
DBA is part of the threat model. It is a recommendation, applied by whoever owns the database.

### MySQL: use `utf8mb4`

On MySQL, the `firm_audit_events` table (and the audit database's default charset) **must be
`utf8mb4`**. A lossy charset silently mangles 4-byte characters — emoji, some CJK — between the
moment the MAC is computed and the moment the row is read back. Under tamper-evidence that
mismatch surfaces as a **false `TAMPERED`** instead of staying invisible. `utf8` (the 3-byte MySQL
alias) is not enough.

## Schema and migration

Turning the feature on requires Alembic migration **`0002`**, which first renames the released
`firm_audits` table to `firm_audit_events` (with its secondary indexes) to match the workspace
`firm_<module>_<entity>` convention, then adds three nullable columns (`entry_id`, `row_mac`,
`key_id`), a unique index on `entry_id`, and the `firm_audit_seals` and `firm_audit_verify_status`
side tables. The rename is in place and preserves existing rows; nullable columns are
zero-downtime. Direct-SQL consumers and least-privilege grants that reference `firm_audits` by
name must be updated to `firm_audit_events`.

The **unique index on `entry_id` is not** zero-downtime by default:

- **PostgreSQL** — the migration builds it with `CREATE UNIQUE INDEX CONCURRENTLY` inside an
  autocommit block, so it does not lock the table.
- **MySQL / SQLite** — the index build is **blocking**, and its duration scales with table size.
  On a large existing table, run migration `0002` in a quiet window.

If you manage the schema yourself (`create_schema=False`), apply migration `0002` as part of the
same rollout that sets `FIRM_AUDIT_KEY` — Phase 1 above.

## See also

- [Configuration](configuration.md) — every `AuditLog(...)` option, including the tamper-evidence
  parameters.
- [CLI](cli.md) — `firm-audit verify` and `firm-audit seal`.
- [Retention & querying](retention-and-querying.md) — how pruning and checkpoint seals interact.
</content>
</invoke>
