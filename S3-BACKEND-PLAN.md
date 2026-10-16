# firm-audit: optional S3 backend and archival storage

Updated: 2026-10-11. Status: implemented on `codex/firm-audit-s3`; see
[S3-BACKEND-VALIDATION.md](S3-BACKEND-VALIDATION.md) for evidence and remaining deployment checks.
The requirements below preserve the approved plan, including its originally proposed APIs.
This document supersedes the earlier cross-package assessment and its archive-first prioritization.

## Scope and outcome

Ship two complementary capabilities inside **firm-audit**:

1. An optional S3 primary backend alongside the existing SQL backend. Applications can run
   stateless Kubernetes replicas against an existing bucket without a database or PVC.
2. Optional archival storage: immutable, verified batches in S3, with explicit export, archive
   reading, restore, and eventually safe SQL archive-before-prune retention.

SQL stays the default and retains its current API and guarantees. S3 primary storage does not
require archival, a SQL index, an exporter, a leader pod, or firm-queue. Archival must also work
for existing SQL users who do not adopt the S3 primary backend.

Queue, cache, channel, contrib, and a general firm-wide backend framework are out of scope.
Limited audit integration in firm-ui, package metadata, tests, and documentation is in scope.
No new service, bucket creation, lifecycle rule, or Object Lock policy is provisioned implicitly.
S3 is assumed to be an existing managed/shared service; operating a new object-storage cluster
is a separate infrastructure responsibility.

## Supported deployment modes

| Mode | Primary write/read store | Archive | Runtime requirements |
| --- | --- | --- | --- |
| Existing SQL | SQLite/PostgreSQL/MySQL | None | Existing configuration, unchanged |
| Database-free audit | S3 event objects | None | Application pods plus S3 |
| SQL with archives | SQL for online history | S3 immutable batches | SQL plus an explicit export job/CronJob |
| S3 with archives | S3 event objects | Separate archive prefix or bucket | S3 plus an optional export job/CronJob |

An archive is a separately readable data set with its own retention and retrieval behavior.
It is not automatically merged into primary history. S3 event storage and S3 archival may share
an account/service, but should use distinct prefixes and permissions; a same-bucket copy is not
independent disaster recovery.

## API and compatibility decisions

All new names below are **proposed**, not currently implemented APIs. Final signatures are
frozen in phase 1 before implementation.

- Keep `AuditLog(database_url=... / engine=...)`, module-level `record(conn, ...)`,
  `AuditLog.record(..., conn=...)`, SQL `history()`, integer IDs, `engine`, and schema Tables
  unchanged. Existing record calls continue returning `None`.
- Add an explicit `S3AuditLog` in an optional module such as `firm.audit.s3`. It accepts bucket,
  prefix, region, optional compatible endpoint, and SDK configuration/client injection. Users
  install `firm-audit[s3]`; default SQL imports must not load or require boto3.
- Reuse reference normalization and the tagged payload codec. Add a small audit-local immutable
  event representation rather than adapting S3 to the core SQL `Dialect` interface.
- Provide preparation and writing as distinct operations: prepare an event once, then submit
  that same prepared event on retry. A convenience `record()` may compose them, but exceptions
  for unknown outcomes must expose a retry token/prepared envelope. Durable retries across pod
  loss require the caller to retain or deterministically reconstruct that identity and content.
- Return an S3 receipt containing stream identity, event identity, object key, version ID when
  available, and content digest. Do not convert S3 identities into fake SQL integer IDs.
- S3 rejects `conn=` explicitly. No cross-system atomicity with application changes is promised.
  Applications needing atomic audit/business updates retain SQL; a transactional outbox for an
  application that already has a database is a separate integration, not a prerequisite here.
- S3 exposes retrieval by receipt/key and a bounded `scan()` API, rather than silently returning
  incomplete results through the existing SQL `history()` list contract. Scan results carry
  items, continuation, scanned counts, and whether the requested scope was exhausted.
- Add audit-owned archive export/read/verify/restore APIs and CLI commands. UI remains a reader
  of those capabilities and never writes directly to audit tables or S3 objects.

## Primary S3 storage contract

### Identity, encoding, and durable writes

Use one immutable object per event initially. This removes the need for local spooling or a
background flush before durability. Keep the existing supported payload types, nulls, reference
labels, and timestamp precision; encode the envelope with an explicit format/codec version.

The prepared envelope contains a configured stream UUID, event UUID, event timestamp, stable
storage partition, and normalized event data. A key can follow
`<prefix>/events/v1/<stream>/<partition-date>/<event-uuid>.json`. The receipt/prepared token
contains the full locator; UUID alone is not a global lookup API. A retry token identifies the
partition as well as the UUID, and neither may change on retry. Never include actor names,
subject names, payloads, or credentials in keys.

Use conditional creation (`If-None-Match: *`). On timeout or a conditional conflict, reconcile
the same key against the exact envelope/digest. An existing identical object is success; a
content mismatch is a conflict. A timeout with unresolved reconciliation is an unknown outcome,
not a definite failure or success. Do not silently generate a new event identity for retries.

A successful return means the remote service acknowledged the event or its matching prior
write was verified. Pod memory is never the only copy of an acknowledged event. Use bounded
SDK retries and timeouts; expose permission, transport, conflict, and unknown-outcome errors.
A bucket outage surfaces to the caller. The library cannot preserve a request killed before
acknowledgement if the caller also loses its retry information.

Dedupe applies while the original key/content is retained. Deleting an object or placing a
delete marker can permit recreation: indefinite dedupe after retention is not an MVP guarantee.
Receipt version IDs must be honored when retrieving protected versions. A digest is an integrity
check, not proof against someone authorized to replace both content and its recorded digest.

### Reading and pagination

- Require a bounded partition/time scope and a maximum object/request budget for scans.
- Support existing scalar filter meanings where possible, applied after reading candidate
  objects. No secondary object indexes or external query service in the first release.
- Define traversal as partition order plus object-key order, not global event/commit order.
  Do not claim that a UUID or client timestamp establishes a globally ordered log.
- Bind opaque continuation tokens to stream, query, ordering, and format version. Resume from
  the exact scan position, including partially consumed LIST pages; do not skip candidates
  when a result limit or request budget is reached. Treat tokens as untrusted input.
- Distinguish end-of-scope from budget exhaustion, and report cost-relevant scan counts.
- Concurrent insertion during pagination is not a snapshot: keys inserted behind the cursor
  may require a fresh scan. This API is not a reliable tailing/stream-consumption interface.
- Boundaries and time semantics must distinguish the storage partition from the event timestamp,
  especially for imported or backdated events. Document how callers locate such events.

The feasibility gate is useful recent history and filtered reads at an acceptable request cost.
If scans fail this gate, revise the supported read contract before release; adding a mandatory
index database would defeat the Kubernetes deployment objective.

## Archival storage contract

### Shared format and publication

Both primary backends export the same versioned, compressed JSON Lines archive format. Preserve
source stream, source identity, original timestamps, codec, labels, and every payload field.
SQL identity is `(stream UUID, SQL integer ID)`; S3 identity includes its stable event locator.
A cloned database that diverges must receive a new stream identity.

A manifest identifies the exact batch and its object keys/version IDs, content digests, event
count, time bounds, source scope, and exact membership or a checksummed membership object.
Use bounded batches and deterministic serialization/compression for retryable publication.

Persist batch identity and immutable input/membership before publishing. Upload data first,
then a conditionally created completion manifest last. Readers accept only complete manifests
and verify referenced content. This is a recoverable publication protocol, not a multi-object
transaction. On retries, compare existing content before accepting it. Incomplete uploads are
orphans, not valid archives; cleanup is separate and explicit.

Overlapping exports can contain the same source event. Archive reading/restore must deduplicate
by source identity, accepting identical content and rejecting conflicting content. Prefer stable
job/batch identities on retries to avoid unnecessary duplicate storage.

### Sources and archive completeness

**SQL:** the first export uses a defined snapshot with pruning disabled, or an explicit
quiesced source. Continuous export later tracks exact membership using audit-owned SQL batch
and membership tables. Discover rows lacking membership repeatedly; never advance a permanent
`MAX(id)` watermark. Lower IDs can commit after higher IDs.

**S3:** start with copy-only export of bounded partition scopes. Persist selection/membership in
S3 so export jobs can resume without a PVC or database. Live LIST pagination does not establish
complete coverage of a changing partition: the receipt describes the observed selected set.
A complete historical export requires a quiesced source/scope or a later proven partition-sealing
protocol. Repeated runs must tolerate late arrivals and overlap. Do not delete source objects
based only on an elapsed time window or completed LIST pass.

For S3 primary storage, source compaction/deletion after export is deferred. Copy-only archives
provide portable snapshots of selected records and a separate retention destination without
introducing unsafe cursor or retry behavior. Optional lifecycle transitions are an infrastructure
policy, not automatic archive publication or verified source coverage.

### Reading, restore, and retention

- Ship archive inspection, verification, and bounded reading before source deletion.
- Restore first to an isolated target via audit-owned APIs. Preserve provenance and timestamps;
  ordinary `record()` is unsuitable because it regenerates identities/timestamps.
- Empty SQL-target restore can preserve integer IDs and must repair generated-ID state through
  the database seam where required. Import into a nonempty target needs explicit identity
  mapping/conflict behavior and is deferred from the first restore release.
- S3 restore preserves the prepared source locator where compatible; cross-backend imports
  preserve source identity separately from destination identity. Do not overwrite on conflict.
- Archive and primary retention are independent and default to no deletion. SQL archive-before-
  prune is an explicit policy added only after delivery/recovery validation.
- Locked objects, noncurrent versions, and manifests need coordinated retention. Batch events
  with compatible retention/hold policies; individual events cannot be removed from an immutable
  batch. Lifecycle execution is asynchronous and must not be treated as exact application TTL.
- Cold storage classes may require a restore operation before reading; return an actionable
  retrieval status rather than treating a temporarily unavailable object as missing/corrupt.
- An asynchronous archive does not protect events lost with the primary before export. Bucket
  replication, backup, account isolation, and recovery objectives remain deployment decisions.

## Implementation phases and acceptance criteria

### Phase 1 — Freeze contracts and prove S3 feasibility

Deliver the event envelope/receipt/scan contracts, provider capability matrix, and a small
prototype of conditional write, reconciliation, direct read, and bounded history.

Measure representative event sizes and rates, concurrent writers, recent-history and subject
search, request counts, and p50/p95/p99 latency. Include reads, verification, retries, encryption,
transfer, retained versions, and archive batch overhead in cost estimates. Provider, region,
workload, acceptable latency, and budget are inputs to these gates, not invented guarantees.

**Exit:** multiple writers and timeout-after-success converge correctly; the selected provider
supports the required primitives; the read/cost contract is acceptable without another service.

### Phase 2 — Production S3 primary backend

Implement the optional client, public S3 audit API, shared normalization/codec extraction,
configuration, errors, packaging, and CLI record/read/scan operations. Keep SQL constructors
and return types unchanged. No generalized storage framework or feature imports into core.

Suggested areas: `audit/s3.py`, `audit/event_format.py`, `audit/object_store.py`, `audit/cli.py`,
package extras and lockfile, `tests/audit/`. Final file boundaries should follow the prototype.

**Exit:** contract tests, collision tests, caller-managed retries, unsupported `conn=` errors,
optional-install tests, and real-provider round-trips pass. Stateless replicas need no database,
PVC, leader, local spool, or export worker to record and retrieve events.

### Phase 3 — Copy-only archives for SQL and S3 sources

Implement the common archive codec, manifests, resumable batch publication, explicit export,
verify, read, and isolated-target restore. Keep source adapters small and audit-local. S3 export
state is stored in S3; SQL export state may use audit-owned tables after migration support exists.
No network I/O while holding database row locks.

**Exit:** export resumes after each crash boundary; incomplete batches are ignored; overlap is
deduplicated; corruption/conflicts fail visibly; round-trip comparison preserves every field and
supported tagged type. S3 source receipts do not overstate completeness under concurrent writes.
All source retention behavior remains unchanged.

### Phase 4 — Continuous SQL archival and safe pruning

Add durable `firm_audit_*` batch/membership state with versioned migrations and indexes. Use
short transactions and the existing locking seam to reserve exact unexported rows and freeze
batch content; upload outside locks. Leases/generations prevent stale workers from acknowledging
reassigned work. A crash after manifest publication but before SQL acknowledgement reconciles
the same batch. Track at-least-once delivery with idempotent publication, not distributed
exactly-once transactions.

The audit table itself is the durable source: SQL recording need not enqueue a best-effort
callback. Module-level and instance-level recording are both covered by discovery of unassigned
committed rows. An outbox inserted on every write is unnecessary unless benchmarks justify it.
Pruning must honor a shared persisted archive policy, including CLI/background callers; an
instance-only flag is insufficient. Upgrade all pruning processes before activating this policy,
pause existing pruning during backfill/cutover, and reject incompatible settings.

Delete only exact verified membership older than the hot-retention cutoff. Pending, failed, or
unverified rows remain. Preserve receipts after hot rows are removed. Independent archive
expiry/deletion invalidates this protection, so archive retention must meet the total required
retention period. Existing deployments not opting into archival retain current pruning semantics.

**Exit:** delayed SQL commits, competing exporters, stale leases, backlog catch-up, and concurrent
prune/export tests pass on live PostgreSQL/MySQL as well as SQLite. S3 failure blocks affected
pruning and exposes backlog; it never causes dropping of unarchived events.

### Phase 5 — Audit dashboard, Kubernetes guidance, and release

Adapt only audit-specific firm-ui configuration and read paths. Keep the existing SQL schema
read surface and views. S3 views show bounded history, details, filters, continuation and scan
limits; unsupported global counts/sorts are absent or explicitly scoped. Archive reads are a
separate view/API, not a hidden scan behind normal history. Other UI modules remain untouched.

Document SDK credential/workload-identity configuration, endpoints, least-privilege writer/reader
roles, timeouts, retries, multi-replica deployments, and optional archive CronJobs. Archiving is
not required by S3 primary mode. Explain optional Object Lock/versioning and policies without
mutating infrastructure implicitly. Surface errors and archive backlog via audit-local callbacks
and CLI status, without a dependency on queue hooks or stdlib logging.

Document API signatures and cookbook examples; update audit docs/nav and regenerate both LLM
files. Wire extras through the meta-package only where consistent with existing package extras.

**Exit:** upgrade/installation docs work, SQL behavior remains compatible, multiple pods survive
rolling restarts and interrupted network requests, and provider support is backed by real tests.

## Validation checklist for implementation

- Existing SQL behavior, both public record paths, reference normalization, typed JSON, integer
  IDs, transaction rollback, history ordering/filtering, SQL schema reads, and opt-in retention.
- S3 conditional create races, changed-content retries, unknown outcomes, concurrent writer pods,
  pod death, credential refresh, permission denial, corruption, and provider-specific errors.
- Scan page boundaries, filter selectivity, empty pages, budget exhaustion, time boundaries,
  backdated imports, malformed/mismatched tokens, and documented concurrent-insertion behavior.
- Archive partial publication, manifest/data disagreement, overlapping runs, resume after every
  boundary, source-version identity, locked data/manifests, cold retrieval, and restore conflicts.
- SQL membership/pruning with delayed commits, no maximum-ID cursor, abandoned reservations,
  competing workers, snapshot/backfill cutover, and a growing backlog during object-store outage.
- Auto-create and Alembic upgrade paths for new SQL archival tables; no SQL initialization in
  S3 mode. Test existing-version upgrades and empty/nonempty restore behavior explicitly.
- Real AWS and each advertised compatible provider; mocks alone cannot certify behavior.
- Run `uv run python -m pytest`, `uv run ruff check packages tests scripts examples`,
  `uv run ty check packages`, and required pre-commit checks. Run documentation generation
  after implementation doc/nav changes. No S3 claims based solely on existing SQL tests.

## Evidence and current status

Code anchors: `audit/log.py`, `audit/events.py`, `audit/schema.py`, `audit/serialization.py`,
`audit/retention.py`, `ui/audit_queries.py`, and `core/dialects/base.py` under their package
source directories. Primary AWS references and compatibility caveats are collected in
[S3-BACKEND-RESEARCH.md](S3-BACKEND-RESEARCH.md). This plan governs scope and sequencing;
the earlier research is supporting evidence, not a competing implementation plan.

The implementation is isolated in a new worktree; unrelated original-checkout edits remain
outside it. Actual provider/deployment validation is recorded separately, without treating
local MinIO evidence as AWS or Kubernetes production certification.

Implementation decision: batch membership is immutable and never reassigned. Competing or
stale exporters retry exactly the same frozen bytes; leases/generations are unnecessary for
this model. Receipts carry the full key locator (including stream/event identity), digest,
and optional version ID. Errors surface as exceptions; backlog is available through
`SQLArchiver.status()` and the CLI. No audit background exporter or callback subsystem is added.

Integration update, 16 October 2026: the current released SQL table is `firm_audit_events`,
with signing/sealing migration `0002`; archival follows in `0003`. Archive exports and native
restores preserve signed row bytes/metadata, and aligned retention enforces both integrity
and archive guards. See the usage guide for the distinction between event archives and
backups of the original seals/anchors. S3 routes share the transport-free mounted dashboard.
