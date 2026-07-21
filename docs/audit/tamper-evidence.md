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
  verifier knows which key to check under. It is not the key and reveals nothing about it.

```python
# Explicit key (overrides the environment). Pass "" to force the feature off.
audit = AuditLog(engine=app_engine, mac_key="a-32-char-or-longer-secret-here!!")
```

### Rotation

Rotate without re-signing — re-signing would require an `UPDATE`, which the append-only contract
forbids. New writes use the new key; verify is given a **keyring** so old rows stay verifiable:

```bash
# Writers carry the new key:
export FIRM_AUDIT_KEY="the-new-32-char-or-longer-secret-value!!"

# Verify is given both, labelled — split on the FIRST '=' (commas inside keys are rejected):
export FIRM_AUDIT_KEYS="2025=the-old-secret...,2026=the-new-32-char-or-longer-secret-value!!"
firm-audit verify --anchor /var/lib/firm/audit.anchor
```

`FIRM_AUDIT_KEYS` entries split on the **first** `=`, so a key value may itself contain `=`; a
comma inside a key value is rejected at parse time with a pointed error (use the keyring form, not
raw commas). Writer and verifier parse with the same function — a parse divergence would
masquerade as tampering. Verify **hard-fails on an unknown `key_id`**: a forged row cannot invent a
`key_id`, because it still needs a valid MAC under a known key.

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
a nightly cron gives full coverage weekly). An edit in an old range is therefore caught by the
*default* run within a bounded, documented delay — not only by a manual `--full`.

The rotation state is **advisory only**: it schedules work but is never trusted for a verdict — the
seal chain and anchor are the sole authority. An attacker who rewrites the state file only reorders
which ranges are checked first; they cannot suppress detection. When verify state and the database
share a host, run a from-genesis `--full` pass periodically anyway.

> **Green is honestly scoped.** The dashboard's integrity strip states the age of the last
> *full-coverage* pass, not just the last run — "green" never silently means "only the tail was
> swept".

## Retention and checkpoints

Pruning deletes old rows, which would read as tampering — so retention and sealing are wired
together with **checkpoint seals** (see [Retention & querying](retention-and-querying.md)):

1. `Retention.run_once` aligns its cutoff to a seal boundary — it deletes only rows in ranges
   **fully covered by seals older than the cutoff**, never partial ranges and never unsealed rows.
2. **Retention only prunes what verifies.** Before deleting a fully-expired sealed range, it
   re-verifies the range — recomputing every row's `row_mac` from its content *and* the range's
   `rows_mac`/`row_count` against the seal, the same check the verifier runs. A range that no
   longer verifies is **refused**: pruning stops there, the checkpoint never advances past it, the
   count lands on `Retention.last_refused_tampered`, and the refusal routes through `on_error`. This
   closes a laundering hole — an attacker who edits an old sealed row with a plain `UPDATE` (no key
   needed) and waits for it to age past `max_age` cannot get a naive prune to delete the evidence
   and checkpoint over it. The refusal stops at the first bad range and repeats every run until an
   operator investigates (`firm-audit verify --full`), so the evidence is preserved, never erased.
   Trade-off: pre-prune verification **re-reads (keyset-paginated) every row it is about to
   delete** — a bounded read cost paid once per prune.
3. After deleting, it writes a `kind="checkpoint"` seal recording `pruned_through_id` and carries
   the chain forward as usual.
4. Verify skips row-recomputation at or below the newest checkpoint but still validates the seal
   chain across it. Rows *above* the checkpoint that go missing remain violations.

This gives retention a hidden dependency on **sealer liveness**: with a stalled sealer, nothing
past the last seal is prunable, so the table can grow past `max_age`. That failure is **loud, not
silent** — `run_once` returns and logs the count of expired-but-unsealed rows it had to skip,
`firm-audit prune` prints it, a skip count above a threshold routes through `on_error`, and
verify's unsealed-tail-age `WARNING` independently flags the stalled sealer.

## Deployment hardening

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
