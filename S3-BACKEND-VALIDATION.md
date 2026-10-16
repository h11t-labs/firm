# firm-audit S3 implementation validation

Validated 11 October 2026 in the managed `firm-audit-s3` worktree, branch
`codex/firm-audit-s3`, based on `8568c4731762279cab0fe89f1bab1385589d5f12`.

## Integration with main — 16 October 2026

Integrated main `5c77af3` into the feature branch. Archive tables now use migration `0003`
after the released tamper-evidence `0002`. The SQL archive preserves stored JSON bytes and
row signatures; archive-before-prune also guards whole signed ranges and their legacy prefix.
S3 UI routes use the transport-free dashboard, with shared mount prefixes, themes, and auth.
The final diff against main excludes inherited cache changes. Both meta-packages expose the
optional SDK consistently; a dedicated SDK-enabled CI job covers the S3/archival surface.

Focused live validation: **83 passed, 3 skipped** on SQLite, PostgreSQL, MySQL, and versioned
MinIO. New regressions cover signed-row restore, sealed-range pruning, a real pre-archive
schema upgrade, and mounted/authenticated routes. Ruff, formatting, full type checking,
pre-commit, eight package builds, and a clean SQL wheel install without boto3 passed.
Independent Standards and Spec reviews found no remaining integration findings.

The complete integrated workspace suite is running; its final result will be recorded here
before this PR is marked ready. The PR remains open and unmerged for the user.

## Original implementation validation — 11 October 2026

The implementation provides an optional database-free `S3AuditLog` beside SQL, immutable
copy-only archives for both backends, cross-backend restores with provenance, continuous
SQL archival with persisted archive-before-prune protection, CLI commands, and audit-only
dashboard views. See [the usage guide](docs/audit/s3-and-archives.md).

## Checks

| Check | Result |
| --- | --- |
| Full suite, SQLite + PostgreSQL + MySQL + MinIO enabled | 1,109 passed, 17 skipped, 8 xfailed; 174.03 seconds |
| Audit/dashboard integration pass before final review fixes | 358 passed, 3 skipped; 63.39 seconds |
| Documentation examples after installation-guide updates | 150 passed |
| `uv run ruff check packages tests scripts examples` | Passed |
| `uv run ty check packages` | Passed |
| `uv run pre-commit run --all-files` | Passed, including broader types and generated-doc hygiene |
| `uv build --all-packages` | All seven wheel/source distributions built |
| Clean default core/audit wheel installation | SQL recording and CLI import work with boto3 absent |
| Browser inspection against local MinIO | S3 history, scan counts, archive navigation and 200-event verified archive rendered |

The full suite includes provider tests against disposable, versioned MinIO
`RELEASE.2025-09-07T16-13-09Z`, PostgreSQL 16, and MySQL 8.4 containers, bound to
localhost. Temporary test containers and the dashboard preview are removed afterward.
Five warnings come from existing queue tests calling `fork()` in a multithreaded Python
process. Skips include backend-specific checks and existing optional tests; expected failures
remain the existing suite's explicit xfails. No failing test is marked expected by this change.

### Recovery and compatibility evidence

- Conditional concurrent publication and exact-content retry convergence against the real SDK
  and MinIO; changed content fails rather than overwriting.
- A subprocess dies immediately after the service accepts PUT but before returning a receipt;
  two fresh processes recover the persisted prepared envelope and converge on the same receipt.
- Partial scan pages, zero matches, budgets, query-bound malformed cursors, and version-pinned
  receipts exercise bounded traversal semantics.
- Staged archive publication resumes after manifest failure; corrupt payloads and conflicting
  overlapping provenance fail visibly. Restores preserve timestamps, tagged payload types,
  native SQL IDs where appropriate, and original provenance across SQL/S3 conversion.
- Original `0001` copy-only export and `0001` to `0002` migrations are covered. Supported audit
  event columns and existing SQL APIs remain unchanged.
- Delayed lower-ID commits are discovered on live PostgreSQL/MySQL. SQLite ID reuse cannot
  inherit an old acknowledgement. Module-level SQL recording is covered without callbacks.
- Twenty contention rounds per SQL backend run four workers/eight attempts per round.
  This exposed and fixed a MySQL repeatable-read race: reservation reads now lock current rows,
  with source skip-locked selection so uncommitted writers do not stall discovery.

## Local measurement

One short smoke measurement used 200 events, four writer threads, and a 500-character data
field against localhost Docker MinIO on the development machine:

| Measurement | Observed |
| --- | --- |
| Recording throughput | 669.1 events/second |
| Write latency p50 / p95 / p99 | 5.47 / 10.42 / 13.64 ms |
| Filtered scan, 20 matches from 200 candidates | 214.29 ms, 202 logical GET/LIST operations |
| Archive publication of 200 events | 0.617 seconds |

These are a single local sample, not an AWS capacity claim, production sizing result, or cost
forecast. One-object-per-event requires one conditional PUT per event; filtered scans read
candidates rather than using indexes. Archives additionally retain their recovery input.
Evaluate service request costs, payload sizes, and filter selectivity for the intended workload.

## Standards

The independent standards review found malformed cursor handling, status query amplification,
conflicting provenance during overlap deduplication, and old-schema copy-export compatibility
issues. Those findings were fixed and the final review reported no remaining concrete findings.
The reviewer inspected the revised SQL locking/recovery paths; live validation is supplied by
the suite above, separately from the review.

## Spec

The independent spec review found missing copy-export pagination, multi-manifest inspection/
reading, cross-backend restore, canonical-envelope enforcement, old-schema export support,
and manifest time bounds. Those findings were fixed and the final review reported no remaining
actionable findings. Immutable batch membership replaces the proposed reassignment/lease model:
workers can only retry identical work, so no generation fence is needed. Exceptions and CLI/
`SQLArchiver.status()` expose errors/backlog; no callback or background exporter subsystem is added.

Final outstanding findings: Standards 0; Spec 0. Deployment validation below remains separate.

## Deployment-specific checks remaining

No AWS account/bucket or Kubernetes rollout was exercised. Before a production release, run
`tests/audit/test_s3_integration.py` against a dedicated AWS test bucket with real workload
identity, and test the actual rolling deployment and durable request-state integration.
Validate provider-specific IAM/credential refresh, encryption, Object Lock/legal holds,
lifecycle retention, locked manifests/data, and cold-tier retrieval. No production IAM,
bucket, lifecycle, hold, or cluster configuration was created or changed.

S3 source deletion/compaction is deferred; copy-only exports keep originals. Scans are bounded
object traversal, without indexed SQL history or snapshot/tailing guarantees. S3 writes do not
join SQL application transactions. Activating SQL archival protection requires upgrading every
pruner and pausing pruning during cutover; asynchronous archives cannot protect unexported
primary data lost before publication.
