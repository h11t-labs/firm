# Changelog — firm-audit

All notable changes to `firm-audit` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

### Added

- Opt-in, three-layer tamper-evidence: per-row `HMAC-SHA256` MACs; independent signed id-range
  seals plus one signed activation marker and append-only retirement-floor advances; and an
  append-only external anchor. The database schema always contains the nullable evidence columns
  and side tables; without a key only the runtime behavior is inert.
- `firm-audit verify`, persisted `firm_audit_verify_status`, and the dashboard integrity panel.
  Findings are bounded and structured; row findings link to `/audit/<id>`.
- `AuditLog(on_finding=...)` emits one structured critical/warning alert per finding run. The
  default sink writes one concise stderr line; sink failures route through `on_error`.
- An optional two-key split keeps the seal/floor/activation key off ordinary writers. Sealing and
  aligned retention run on the seal-key host; a signer mismatch is a loud no-op.
- Role-scoped retired-key archives. Retired row keys validate rows only; retired seal keys validate
  Layer-2 records. Single-key rotation requires the old secret in both archives because it signed
  both roles. Verification, anchor healing, and retention consume the archives.
- Stateless partial verification: every run checks always-on invariants, the tail, newest range,
  and `ceil(range_count / verify_cycle)` date-selected old ranges. Only `--full` guarantees full
  range coverage.
- Opt-in in-process verify loop (`background_verification=True`, `verify_interval`,
  `verify_full_every`) — a tail verify on a timer plus a periodic `--full`, each firing
  `on_finding`, mirroring the seal/retention loops. Verification can also be scheduled externally
  (a `firm-queue` recurring task or cron); see the docs.
- `firm-audit anchor-compact` rotates a mutable anchor to one signed coverage/floor `CHECKPOINT`;
  verification accepts full and compacted anchors with the same monotonic-watermark semantics.
- A thin `Signer` protocol and the current `HmacSigner` centralize signing/verification without
  adding an algorithm configuration surface.

### Security (pre-release hardening of the unreleased tamper-evidence layer)

- Verification and retention share one exact range classifier. Altered, missing, surplus,
  relocated, unsigned, or otherwise invalid rows make a sealed range tampered; retention refuses
  rather than laundering the evidence.
- Unknown Layer-2 signers are tampered findings, including row-key-signed forgeries. An unknown row
  key raises `VerifyError` only when it is the sole obstacle; co-occurring tampering still returns
  `tampered` and fires `on_finding`.
- Activation uses the highest settled NULL-MAC row as its boundary, so pre-activation keyed rows
  remain sealable. Verification acquires its database snapshot with the first side-table query,
  then reads the anchor; retention uses the same ordering.
- Anchor verification is now an O(1)-memory monotonic read: mature SEAL coverage and authentic
  FLOOR maxima replace capped per-event reconciliation. Appended junk cannot evict evidence;
  malformed lines are skipped with one warning; a younger-than-grace seal cannot contradict an
  older database snapshot.
- With an anchor, its coverage watermark catches a wiped side table and its floor watermark
  remains authoritative even when the DB floor row is gone. Without an anchor, persisted
  `sealing_observed` memory detects a later side-table wipe without false-alarming a genuinely
  never-sealed growing log; total DB+status wipe remains explicitly outside no-anchor guarantees.
- Retention refuses when an old seal key is unavailable, or when a key is configured but no
  activation exists. Aligned pruning retries serialization/deadlock failures and fsyncs the FLOOR
  anchor append before the database commit. Sealing and aligned pruning serialize on the
  never-deleted activation row (`BEGIN IMMEDIATE` on SQLite, `SELECT ... FOR UPDATE` on
  PostgreSQL/MySQL), closing stale-HWM range reuse.
- Seal-key rotation no longer disables sealing: existing Layer-2 signers may resolve through the
  current or retired seal keyring; unknown and row-only signers are still refused.
- Side-table reads use bounded keyset pages; row and anchor scans are streaming; findings and
  unresolved-row identifiers are capped. Oversized attacker-written cells are bounded before MAC
  recomputation, and `MemoryError` becomes a persisted tampered result rather than escaping.
- A broken verify-status sink routes through `on_error` without masking the returned integrity
  report or its alert.
- The pruned region is probed on every run: a row at or below the signed floor is tampered.
- **Over-length signed values fail loud at `record()` time.** Under a key, a value longer than its
  column (`VARCHAR(255)` scalars, 65535-byte `TEXT` JSON payloads) would be silently truncated by a
  non-strict MySQL and make the untouched row verify `TAMPERED`; it now raises a `ValueError` before
  the insert. The key-less write path is unchanged (byte-identical to before).
- **`key_id` collisions are a hard error, not a silent identity collapse.** Two *distinct* configured
  keys sharing an 8-hex `key_id` now fail loudly (at startup for the row/seal pair, at verify for a
  colliding retired key) instead of shadowing one another; and whether a two-key split is in force is
  decided by the secret, not the `key_id`, so a collision can no longer downgrade a split to
  single-key.
- **Keyring comma contract corrected.** A comma is always an entry delimiter; a secret cannot
  contain one. The parser is fail-closed (never merges two distinct secrets), and the docstring no
  longer over-promises a rejection it cannot make for a well-formed `,label=secret` tail.
- **Mass tampering can no longer OOM verify before it alerts.** Verify capped its findings only at
  serialize time; it now bounds the in-memory findings list during accumulation (the exact counts are
  kept separately), so a million-row tamper still persists the `TAMPERED` status and fires
  `on_finding`, with an honest "+N more" overflow count.
- **Dashboard integrity status is no longer spoofable, DoS-able, or falsely green** (firm-ui). The
  panel reads the verifier's single canonical status row by its fixed id instead of the newest by
  `ran_at` (an attacker could otherwise insert a future-dated `ok` row and pin it green); the
  `affected_identifiers` JSON parsers reject oversized input and survive deeply-nested input instead
  of 500-ing every render; and on a truncated tamper run the per-row table degrades sealed rows it
  cannot vouch for to an honest "not individually verified" mark rather than a green checkmark.

### Changed

- **Breaking:** the audit table is renamed `firm_audits` → `firm_audit_events` to match the
  workspace `firm_<module>_<entity>` table convention. Migration `0002` renames the table and its
  secondary indexes in place (existing rows preserved). Direct-SQL consumers, least-privilege
  `GRANT` recipes, and any code referencing `firm.audit.schema.audits` (now
  `firm.audit.schema.audit_events`) must be updated. A database migrated from 0.1.0 keeps its
  original Postgres sequence name `firm_audits_id_seq`.

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed, append-only audit log (original to firm — no Rails
  counterpart) running on SQLite, PostgreSQL, or MySQL/MariaDB.
- Opt-in retention, `history()` querying, and a `firm-audit` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-audit-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-audit-v0.1.0
