# Tamper-evidence

An audit log is only as trustworthy as its worst day — the day someone with database access
edits a row, deletes an inconvenient event, or drops the table and swears it never happened.
firm-audit's tamper-evidence layer is **opt-in** and makes those changes *detectable*: not
prevented (anyone with `DELETE` can delete), but impossible to do without leaving a mark that
verification will find.

It is off until you configure a key. **Without `FIRM_AUDIT_KEY`, the runtime behavior on this page
is inert**: writes leave the evidence columns NULL and reads behave as before. The nullable columns
and two side tables are always part of the migrated schema, independent of key configuration.

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
certificate-transparency proofs. It is **not** designed to stop an attacker who holds the
database *and* the secret key *and* the external anchor, and it does not prevent destruction:
someone with access can always `DROP TABLE`; the guarantee is that this cannot go **unnoticed**.
Rows stay plaintext — this is integrity, not confidentiality.

### What the anchor is worth depends on where it lives

Detection of deletion, tail-truncation, and wholesale reset needs external memory. The anchor is
therefore **mandatory for the full guarantee**, and only works when this hard requirement holds:
**DB compromise must not grant anchor write.** Placement is the deployer's choice:

| Safety level | Placement | Guarantee / tradeoff |
|---|---|---|
| Weakest | **Local file on the app host** | Safe against a database-only compromise when the DB is managed elsewhere (for example RDS) and those credentials cannot write the app filesystem. Risky on one all-in-one host |
| Better | **Separate host or append-only mount** | The DB attacker is outside the anchor filesystem's write boundary |
| Strongest | **S3 Object Lock / WORM** | Storage enforces immutability; use object lifecycle expiry rather than rewriting old objects |
| Documented-weak mode | **No anchor** | Layers 1+2 detect modification and deletion under surviving seals, but not a total wipe of hidden events plus seals plus verify status |

Whoever can compromise the database must not be able to write the anchor. A local path is not
automatically unsafe; the trust boundary matters. Conversely, an off-host sink reachable with the
same compromised credentials is not independent.

## The three layers

Protection is split so that the fast path (writing rows) stays lock-free and multi-writer, and the
slow path (proving ordering) needs no coordination.

| Layer | Mechanism | Catches |
|---|---|---|
| **1 — row MAC** | every row self-authenticates with `HMAC-SHA256(key, canonical(row))` | modify, forge, replay |
| **2 — seals** | a background loop independently signs exact id ranges | delete/insert inside a range, seal-tampering |
| **3 — anchor** | monotonic signed coverage/floor watermarks live outside the database | seal-tail truncation, deletion, table reset |

**Layer 1 — per-row MAC.** At insert time, when a key is configured, the writer generates a ULID
(`entry_id`, unique-indexed — identity and anti-replay) and stores `row_mac`, a keyed HMAC over the
row's canonical bytes. It depends on **nothing but the row itself**: no locks, no reads, no extra
round-trips, so N app instances insert exactly as they do today. Editing any column breaks its MAC;
an out-of-band insert has no valid MAC; a replayed row collides on `entry_id`. In the *unsealed*
tail a valid `(entry_id, row_mac)` pair is modification- and forgery-protected but not yet pinned
to its position — id-to-content binding arrives with the seal.

**Layer 2 — seals.** A background `SealLoop` periodically hashes one contiguous settled range
`(from_id, to_id]` into a `firm_audit_seals` row. `rows_mac` covers every `(id, row_mac)` pair in id
order; `seal_mac` signs the range bounds, `row_count`, `rows_mac`, time, and signing `key_id`. Each
seal stands alone. Deleting, inserting, or moving a row inside its range breaks
`row_count`/`rows_mac`; editing the seal breaks its own MAC. Adjacent seals must be contiguous.
Any instance may run the loop — the unique `from_id` constraint arbitrates races, so no leader
election is needed.

The first sealer pass also writes one signed `kind="activation"` record. Its boundary is the
highest NULL-MAC event id already outside the grace window (or zero when there are none):
pre-key rows at or below it are the legacy prefix and are never sealed, while every keyed row is
above the boundary and is sealed, including keyed rows written before activation. Retention later
records the pruned prefix with signed
`kind="floor"` advances. Activation, seals, and floors use the seal key.

**Layer 3 — anchor.** Every signed record also leaves the database — appended to a local file
and/or handed to a callback (ship it to S3, a second database, a log pipeline). The file is
append-only and has one canonical line per event:

```text
<sealed_at> SEAL <from_id> <to_id> <seal_mac>
<retired_at> FLOOR <through_id> <floor_mac>
<at> ACTIVATION <boundary_id> <activation_mac>
<at> CHECKPOINT <coverage_id> <floor_id> <checkpoint_mac>
```

Verification streams the file once in O(1) memory. It keeps only (1) the greatest mature
`SEAL.to_id` coverage and (2) the greatest authentic `FLOOR.through_id`; a signed `CHECKPOINT`
contains the same values after compaction. Coverage below the anchor watermark is `TAMPERED`.
Rows at/below the floor are `TAMPERED`, while missing rows and seals there are an authorized
prune. Malformed or partial lines cannot lower a maximum, so they are skipped and collapsed into
one `WARNING`, never treated as tampering by themselves.

Seal and activation emission happens after the database commit and is best-effort. Before new
work, the sealer only ensures that the current maximum seal coverage is anchored. A floor is
stricter: retention appends and `fsync`s it before the prune commits; append failure refuses the
prune.

`on_anchor` is a write-only delivery callback. If it is your only sink (for example S3 or another
database), materialize that history as the canonical line format and supply it to verification via
`anchor_path`; the callback alone cannot be read back to detect truncation or reset.

### Anchor growth, compaction, and rotation

File length does not affect verification memory or correctness. On a mutable local/separate-host
anchor, compact it to one signed watermark when needed:

```bash
firm-audit anchor-compact --database-url "$FIRM_AUDIT_DATABASE_URL" \
  --anchor /var/lib/firm/audit.anchor
```

The command first appends and `fsync`s a signed `CHECKPOINT`, then removes strictly older lines
under the same file lock. Verification accepts the compacted file like the full history. Keep the
current and retired seal keys available while compacting and reading checkpoints. For S3 Object
Lock or other WORM storage, do not rewrite an object: rotate to new objects and use lifecycle
expiry for old ones. Growth is harmless to verifier memory.

## Rolling it out — key first, then sealing

Key presence and sealing are **two separate switches, enabled in order**, because enabling a key
is never atomic across a fleet.

1. **Phase 1 — deploy the key everywhere.** Set `FIRM_AUDIT_KEY` on every instance. Rows start
   carrying MACs. No seal boundary exists yet, so a straggler instance still writing MAC-less rows
   is harmless — it is not yet an alarm.
2. **Phase 2 — enable sealing**, once every writer carries the key: `background_sealing=True` (or
   run `firm-audit seal`). The first sealer pass writes an explicit signed **activation marker**
   whose boundary is the highest pre-key (NULL-MAC) row id outside the grace window, or zero when
   there are none. From then on, a row *above* the boundary
   with no MAC is `TAMPERED` (a configured writer never produces one), while rows at or below it
   are the legacy `UNPROTECTED` set.

Doing it in the other order would make every rollout flash red while stragglers catch up. The
sealer restates this order in its startup log.

> Pre-existing rows, written before the key was configured, verify as **UNPROTECTED** — reported
> once as a count, never as tampering.

## Sizing the grace window

Seals only cover rows older than a **grace window** (`grace`, default 60 s). The normal
`AuditLog.record()` path omits `conn=` and writes durably in its own short transaction, so the row
is settled well before its range is eligible. The window also absorbs ordinary scheduling and
clock skew between instances.

If you deliberately pass `conn=` (or call module-level `record(conn, ...)`) for same-transaction
atomicity, **`grace` must exceed the longest audit-recording transaction plus expected
inter-instance clock skew**. There is no late-commit
exception: a row that appears inside an already sealed range changes the exact signed membership
and is permanently `TAMPERED`, even when its row MAC is valid. It is stranded below the seal high
water mark: no later sealer pass and no `verify --full` run can self-heal it. Each range is exactly
the rows its `row_count`/`rows_mac` signed; there are no recorded gaps or lenient classification.
`AuditLog` emits a startup hint restating this when sealing is enabled.

`grace` also bounds the anchor's truncation guard: the coverage watermark only enforces seals older
than `grace` (so a just-committed seal that a verifier's snapshot cannot yet see is not read as a
truncation). The flip side is that deleting a seal **younger than `grace`** is not caught by the
watermark until it ages — and if the sealer's clock runs ahead of the verifier's by δ, that
unenforced tail widens to `grace + δ`. Keep `grace` small (and clocks in sync) so this recent-tail
exposure stays narrow.

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
*after* the job's transaction commits. `grace` then stays tight (60 s) without risking a row
appearing inside a range whose exact membership was already sealed.

## Key management

The key comes from `FIRM_AUDIT_KEY` (or `AuditLog(..., mac_key=...)`). No key means the feature is
off: columns stay NULL and everything behaves as it did before tamper-evidence existed.

- **The key must be a UTF-8 string of at least 32 characters.** A shorter key is a **hard error at
  startup**, not a warning — a weak key silently voids all three layers, so it fails loudly
  instead. Empty or absent means the feature is simply off.
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
still verify. Retired keys live in two **role-scoped** archives. Verification and retention read
them; the sealer accepts old Layer-2 records signed by the retired seal archive while writing new
records with the current seal key. Writers never use retired keys to sign new evidence:

- **`FIRM_AUDIT_RETIRED_KEYS`** — retired **row** keys. Eligible to validate row MACs only, and
  **never** a seal, in any mode.
- **`FIRM_AUDIT_RETIRED_SEAL_KEYS`** — retired **seal** keys. Eligible to validate Layer-2 records
  only. In single-key mode the same old secret also belongs in the row archive.

The archives never hold the *new* key — writers pick that up from `FIRM_AUDIT_KEY` /
`FIRM_AUDIT_SEAL_KEY` alone. Where the **old** key goes depends on what it signed:

| Deployment | Key you rotate | New key → | Retire the old key into | Its old objects that still verify |
|---|---|---|---|---|
| Single-key | the one key | `FIRM_AUDIT_KEY` (every writer) | **both** `FIRM_AUDIT_RETIRED_KEYS` and `FIRM_AUDIT_RETIRED_SEAL_KEYS` | its rows **and** seals |
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
signed by a key that is not a *seal* key (current or retired) is a `TAMPERED` unverifiable-signer
finding, never a laundered OK.

Both archives split each entry on the **first** `=`, so a secret may itself contain `=`. A comma is
**always an entry delimiter** — a secret cannot contain one: `id1=A,id2=B` is two keys and is
byte-identical to a lone key whose secret were `A,id2=B`, so the two cannot be distinguished. A
comma that yields a malformed fragment (no `=`, empty label, or a too-short secret — the common
accidental case) is rejected with a pointed error; a comma followed by a well-formed `label=secret`
is taken as a separate key. Either way parsing is **fail-closed** — it never silently merges two
distinct secrets into one identity, and a genuine `key_id` collision between the parsed keys is a
hard error. **Do not put a comma in a secret;** use a longer comma-free random value. Writer and
verifier parse secrets with the same function — a parse divergence would masquerade as tampering.
Verify hard-fails on an unknown **row** `key_id` only when it is the run's sole obstacle. If another
finding proves tampering, that verdict wins and the alert still fires. Unknown Layer-2 signers are
always `TAMPERED` findings.

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
  `seal_mac`, including range seals, activation, and floors); ordinary app instances keep only
  `FIRM_AUDIT_KEY` and sign just their row MACs.
- After the split, an attacker who compromises an app instance holds only the row key and can forge
  at most an individual **unsealed** row — the independent seals are out of reach. Even editing a
  sealed row and recomputing its `row_mac` *and* the seal's `rows_mac`/`seal_mac` under the row key
  is caught: verify checks seals under the seal key, and refuses the row key as a seal signer.

```bash
# App instances (row MACs only):
export FIRM_AUDIT_KEY="the-32-char-or-longer-row-secret-value!!"

# Sealer / verifier hosts also carry the seal key:
export FIRM_AUDIT_SEAL_KEY="a-different-32-char-or-longer-seal-secret"
```

The verifier needs **both** keys (rows are checked under the row key, seals under the seal key);
configure `mac_key`/`FIRM_AUDIT_KEY` *and* `seal_key`/`FIRM_AUDIT_SEAL_KEY` on it. If verify meets a
seal signed by a key it holds only as a *row* key — the current row key, or a retired one from
`FIRM_AUDIT_RETIRED_KEYS` — it reports a `TAMPERED` unverifiable-signer finding. Retention is more
conservative: an unavailable current or retired seal key causes a loud no-op, never a prune.

**This is opt-in and the default is unchanged.** Leave `FIRM_AUDIT_SEAL_KEY` unset (or set it equal
to `FIRM_AUDIT_KEY`) and the seal key *is* the row key: every instance may seal, one key signs
everything, and behavior is byte-identical to single-key mode.

**The tradeoff is honest:** the split turns "any instance may seal" into a **designated sealer
role**. Sealing and retention's floor advance both need the seal key, so in a split deployment they
run on a sealer-role host, not just anywhere. A host without the seal key that tries to seal is the
usual loud no-op after a seal-key-signed record exists; a host without it that tries to prune the
aligned path **refuses loudly**
(see [Retention and the signed floor](#retention-and-the-signed-floor)) rather than sign a floor
with the wrong key. You gain a smaller blast radius; you pay with a second secret to manage and a
role to place.

The first-ever activation has no existing signer from which to infer deployment intent. In a
two-key deployment, run both the sealer and retention only on the seal-key host from the start.
After a seal-key rotation, mixed current+retired seal `key_id` history is expected. The sealer
refuses only an unknown signer or one available solely as a row key.

## Verifying

`firm-audit verify` (and `AuditLog.verify()`) check Layer 1 rows (keyset-paginated on `id` for
bounded memory), independent Layer 2 seals plus the signed activation/floor records, and the Layer
3 anchor. Every finding carries one of four **verdict classes**:

| Verdict | Meaning |
|---|---|
| `OK` | rows, seals, activation/floor records, and anchor all consistent |
| `WARNING` | liveness or non-authoritative input issue: old unsealed tail, stale/missing anchor, unreadable anchor lines, or a bounded side-table scan |
| `UNPROTECTED` | NULL-MAC rows at or below the activation boundary — written before the key existed |
| `TAMPERED` | invalid/missing MAC after activation, exact-range mismatch, invalid/non-contiguous seal or marker, a row at/below the floor, or an anchor contradiction |

A signed database record an attacker can edit but not re-sign is a tampering **finding**, never an
uncaught parse exception. Malformed anchor lines are warning-only because they cannot lower a
monotonic maximum. Verify persists the result, so malformed attacker-controlled content cannot
freeze the dashboard's last status at `OK`. An unknown row
`key_id` becomes `VerifyError` only when no tampered finding exists; otherwise tampering takes
precedence and `on_finding` still fires.

> **Activation-boundary caveat.** The boundary is the highest settled NULL-MAC row id, not the
> highest id overall. This keeps every keyed row — including rows written before activation —
> above the boundary and eligible for sealing. A NULL `row_mac` at or below the boundary remains
> `UNPROTECTED`; above it, NULL is `TAMPERED`. If unkeyed and keyed writers interleave during
> rollout, a keyed row whose id falls below a later straggler NULL-MAC row can still land in the
> unprotected prefix. Phase 2 must therefore start only after every writer carries the row key.

### Exit codes

- **Exit 0** — `OK` or `UNPROTECTED` only. `WARNING`s also exit 0, but they print.
- **Non-zero** — any `TAMPERED` finding.
- **Non-zero (anchor exception)** — when `--anchor` is given and the newest anchor is older than
  `anchor_max_age` (default: 3× the seal interval, configurable). The silently-truncatable window
  between the last anchored event and the newest database record is the one thing only Layer 3
  guards; letting it grow unbounded behind an exit-0 warning would quietly degrade the only
  guarantee the anchor exists to give. Seal writes stay best-effort and maximum coverage heals on
  a later sealer pass; floor writes are a hard pre-prune gate.

The evidence scan is read-only and runs anywhere the key is available; persisting
`firm_audit_verify_status` requires a write grant. Inside the snapshot transaction it reads the
seal side table first, which actually acquires the `REPEATABLE READ` snapshot on Postgres/MySQL
(and the WAL snapshot on SQLite), and only then reads the external anchor. A concurrent legitimate
prune therefore cannot make verification compare an anchor view with a not-yet-acquired database
snapshot and report a false `TAMPERED`.

### Stateless partial coverage

Re-reading every sealed range on every run is expensive; only reading the tail would leave old
row edits unchecked. The default run verifies every always-on invariant (all signed-record MACs,
activation/floor validity, seal contiguity, anchor watermarks, pruned-region emptiness, duplicate
`entry_id`s, and tail liveness), recomputes the unsealed tail, and selects
`ceil(range_count / verify_cycle)` ranges from the day's distance since 1970. It always adds the
newest range if that slice did not select it. The choice is deterministic and **stateless**: there
is no cursor, state file, or mutable rotation position.

`verify_cycle` is a cost divisor, not a period: larger values recompute fewer old ranges per run.
Because the date-derived start advances through id-range positions, the conservative worst-case
rotation bound is `n_ranges` days, not `verify_cycle` days.

This keeps the default cost bounded, but **only a periodic `--full` guarantees every sealed range
is recomputed**. A skipped schedule can skip a date-selected slice too; do not treat partial runs as
proof that every old row was recently checked. Run `--full` on a schedule appropriate to the risk
and data volume.

> **Green is honestly scoped.** The dashboard's integrity strip states the age of the last
> *full-coverage* pass, not just the last run — "green" never silently means "only the tail was
> swept".

## Alerting / log stream

A verify run that *detects* tampering is a signal, not just a return value. So besides attempting to persist
the outcome (for the dashboard) and returning an exit code (for a cron), every run whose outcome is
`TAMPERED` or `WARNING` fires an **`on_finding`** hook — **once per run, after status persistence
is attempted** — with a structured `IntegrityAlert` (severity `critical` for tampered, `warning` for
warning; the outcome, the counts, the affected identifiers, and `ran_at`). This is the *in-process
event path*; a scheduled `firm-audit verify` (cron) plus its **exit code** is the *batch path*.

Wire a sink to forward alerts to Datadog / Loki / a JSON logger:

```python
from firm.audit import AuditLog, IntegrityAlert

def to_logs(alert: IntegrityAlert) -> None:
    # alert.severity is "critical" (tampered) or "warning"; never the key or row content.
    my_logger.error("audit integrity", extra={"severity": alert.severity, **alert.__dict__})

audit = AuditLog(engine=app_engine, mac_key="...", on_finding=to_logs)
audit.verify(full=True)  # fires to_logs on a tampered/warning outcome
```

**The default is present, not silent.** With no `on_finding` configured, a detection writes **one**
concise high-severity line to stderr (the project bans stdlib logging, so this mirrors
`on_error`'s stderr route) — so even a stock deployment's logstream shows it:

```text
firm-audit: CRITICAL tamper detected — 2 findings, affected: #42 invoice.paid, sealed range (11, 20] (verified 2026-07-21 03:00:00)
```

`OK` and `UNPROTECTED` runs stay silent; the `error` outcome (verify itself could not check — e.g.
an unknown `key_id`) is surfaced by the raised `VerifyError` and `on_error`, not `on_finding`. To
**mute** the default line, pass a no-op (`on_finding=lambda alert: None`); to redirect it, pass your
own sink. A sink that raises is routed to `on_error` and never crashes verification.

This fires for both `AuditLog.verify()` and the CLI `firm-audit verify` (the CLI still prints the
per-finding messages to stdout and returns the exit code; the stderr line is the log-pipeline
event). `on_finding` is what turns each run into an event — so verification has to actually *run*
on a schedule, which is the subject of the next section.

## Scheduling verification — run it continuously

**You want verification running all the time.** Tamper-evidence is not passive: a modification is
only detected the next time `verify` recomputes over it, and `on_finding` only fires on a run that
detects something. A log that is written and sealed but never verified is exactly as forgeable in
practice as one with no evidence at all — the signatures are there, but nobody is checking them. So
treat verification like a health check: run it on a short interval, and run `--full` on a longer one.

Unlike the opt-in in-process `SealLoop` / `RetentionLoop`, firm-audit does **not** bundle a
`VerifyLoop`. That is a packaging choice, not advice to verify rarely: verify is read-only, and its
cadence, its `--full` schedule, its alert routing, and *where* it runs (often a separate
verifier/anchor host, not an app instance) are all deployment decisions. Drive it from a scheduler
you already run. Two natural options:

**Option A — a recurring job on `firm-queue`** (dogfooding: use firm's own queue). `firm-queue`
ships cron-scheduled [recurring tasks](../queue/recurring.md), so a verifier is a `@bq.job` plus two
`RecurringTask`s:

```python
import firm.queue as bq
from firm.audit import AuditLog, IntegrityAlert
from firm.queue.scheduler import RecurringTask

def to_logs(alert: IntegrityAlert) -> None:
    my_logger.error("audit integrity", extra={"severity": alert.severity, **alert.__dict__})

@bq.job(queue="audit")
def verify_audit(*, full: bool = False) -> None:
    audit = AuditLog(engine=verifier_engine, mac_key="...", anchor_path="/var/lib/firm/audit.anchor",
                     on_finding=to_logs)
    report = audit.verify(full=full)          # to_logs fires on a tampered/warning outcome
    if report.exit_code != 0:
        raise RuntimeError(f"audit verify: {report.outcome}")   # job failure surfaces in the queue

tasks = [
    RecurringTask(key="audit-verify-tail", schedule="*/15 * * * *", job=verify_audit),
    RecurringTask(key="audit-verify-full", schedule="0 3 * * *", job=verify_audit, kwargs={"full": True}),
]
# Hand `tasks` to your SupervisorConfig(recurring=tasks); the scheduler enqueues them on schedule
# and the unique index on recurring_executions dedupes across schedulers.
```

The frequent `*/15` run does the always-on invariants plus the rolling slice; the nightly `--full`
guarantees every sealed range is recomputed (see [Stateless partial coverage](#stateless-partial-coverage)).
Detection reaches your logs two ways: `on_finding` → `to_logs`, and the raised failure → the queue's
own failed-job path and alerting.

**Option B — an external scheduler.** If you would rather not run verification inside the queue, any
cron / systemd timer / CI cron works the same way, using the exit code as the gate:

```cron
*/15 * * * *  firm-audit verify --database-url "$DB" --anchor /var/lib/firm/audit.anchor || alert
0    3 * * *  firm-audit verify --database-url "$DB" --anchor /var/lib/firm/audit.anchor --full || alert
```

Either way, the anchor-age rule (a stale anchor forces a non-zero exit) means a verifier that stops
running, or an anchor sink that stalls, eventually trips the same alarm as tampering — silence is not
mistaken for health.

## Retention and the signed floor

Pruning deletes old rows, which would otherwise read as tampering. Retention therefore advances one
logical, signed **retirement floor** (append-only `kind="floor"` records; the highest valid advance
wins). See [Retention & querying](retention-and-querying.md).

1. `Retention.run_once` aligns its cutoff to a seal boundary — it deletes only rows in ranges
   **fully covered by seals older than the cutoff**, never partial ranges and never unsealed rows.
2. **Retention refuses to prune what verify would call `TAMPERED`.** Before deleting each expired
   range, it rechecks the independent seal's own MAC and runs the **same exact range classifier as
   verify**: every row MAC plus the range's `rows_mac`/`row_count`. Any surplus, missing, altered,
   or moved row fails; there is no late-arrival exception. Pruning stops at the first bad range,
   `Retention.last_refused_tampered` records the refusal, `on_error` fires, and every row remains
   for `firm-audit verify --full` and investigation. The read is keyset-paginated but must revisit
   every row about to be deleted.
3. Retention signs the next floor (`through_id`, retirement time, and signing `key_id`). Every
   configured anchor sink must accept the `FLOOR` event **before** the database transaction can
   commit; the file append is flushed and `fsync`ed, and a sink failure refuses the prune. The floor row, event deletion, and deletion of fully
   retired covering seals then commit together in one write transaction. Serialization/deadlock
   aborts are retried a bounded number of times. Floor records themselves are append-only and
   monotonic.
4. Verify honors the highest authentic database or anchor floor. Seals entirely at/below it may
   legitimately be absent. Above it, seals must
   tile contiguously from `max(floor, activation boundary)`. Verify also probes the retired prefix
   on every run: any surviving or reinserted row with `id <= floor` is `TAMPERED`.

This gives retention a hidden dependency on **sealer liveness**: with a stalled sealer, nothing
past the last seal is prunable, so the table can grow past `max_age`. That failure is **loud, not
silent** — `run_once` returns and logs the count of expired-but-unsealed rows it had to skip,
`firm-audit prune` prints it, a skip count above a threshold routes through `on_error`, and
verify's unsealed-tail-age `WARNING` independently flags the stalled sealer.

The floor is seal-side evidence, so in a
[two-key deployment](#two-key-split-a-separate-seal-key-optional-hardening) **retention needs the
seal key**. Run pruning on a sealer-role host that has `FIRM_AUDIT_SEAL_KEY`. On a host without it
— one carrying only the row key — `run_once` **refuses the whole aligned prune**: it deletes
nothing, sets `Retention.last_refused_no_seal_key`, routes the refusal through `on_error`, and
`firm-audit prune` prints it. (In single-key mode the seal key *is* the row key, so this never
triggers and pruning is unchanged.)

If a seal key is configured but no activation exists while rows are already older than the
retention cutoff, retention refuses plain pruning. This prevents an emptied side table from silently
downgrading a keyed deployment to unguarded age deletion.

Without an anchor, verify persists a `sealing_observed` fact the first time it sees authentic
activation/seal coverage. If events later remain while those records vanish, verification reports
`TAMPERED`; a genuinely never-sealed growing keyed log stays `OK`. This status lives in the same
database and is therefore only a limited guard: **no-anchor mode cannot detect a total wipe of the
events being hidden plus the seal side table plus verify status.** The anchor is the independent
memory that closes that gap and is mandatory for the full deletion/truncation guarantee.

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
GRANT INSERT, SELECT, DELETE ON firm_audit_seals TO firm_retention;  -- floor + retired seals
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
`key_id`), a unique index on `entry_id`, and two side tables. `firm_audit_seals` stores three signed
record kinds (`seal`, `activation`, `floor`) in one id-based schema: `from_id`, `to_id`,
`row_count`, `rows_mac`, `seal_mac`, `sealed_at`, and `key_id`. `firm_audit_verify_status` stores
the latest result and last explicit full-coverage time; partial verification has no cursor or state
columns. The rename is in place and preserves existing rows; nullable event columns are
zero-downtime. Direct-SQL consumers and least-privilege grants that reference `firm_audits` by name
must be updated to `firm_audit_events`.

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
- [Retention & querying](retention-and-querying.md) — how pruning and the signed floor interact.
