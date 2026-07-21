# Changelog — firm-audit

All notable changes to `firm-audit` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

### Added

- Opt-in tamper-evidence: per-row `HMAC-SHA256` MACs (Layer 1), a chained seal log over ranges of
  sealed rows (Layer 2, `firm_audit_seals`), and a `verify` pass with a dashboard integrity panel
  backed by `firm_audit_verify_status`. Inert without a configured `FIRM_AUDIT_KEY` — behavior and
  schema semantics are unchanged for a key-less deployment.
- **Structured, linkable verify findings on the dashboard.** The verify-status row's
  `affected_identifiers` now carries a JSON list of the top-N (20) tampered findings — each with its
  verdict, human message, display label, and (for a row-level finding) the numeric audit-event `id`
  — instead of one flattened label string. The TAMPERED banner surfaces the real per-finding
  "what/why" and links each affected record into `/audit/<id>`. No schema change (the existing
  `Text` column is reused); a clean run still leaves it NULL.
- **`on_finding` — a high-severity alert on detection.** A verify run whose outcome is `TAMPERED`
  (severity `critical`) or `WARNING` fires `AuditLog(on_finding=...)` once, after the status row is
  persisted, with a structured `IntegrityAlert` (severity, outcome, counts, affected identifiers,
  `ran_at`) — so a scheduled or in-process verify emits a routable event to the operator's log
  pipeline (Datadog/Loki/JSON), not just a return value. Fires for both `AuditLog.verify()` and the
  CLI `firm-audit verify`; never for `ok`/`unprotected`. With no sink configured, the default writes
  **one** concise high-severity line to stderr (no stdlib logging), so a stock deployment's
  logstream shows it; pass a no-op to mute it. A failing sink routes to `on_error` and never crashes
  the read-only verify. There is deliberately no `VerifyLoop` yet — cron/CLI (batch, via exit code)
  or a caller-run loop is the cadence; `on_finding` is the in-process event path.
- Optional **two-key split** for tamper-evidence: a separate seal key (`FIRM_AUDIT_SEAL_KEY` /
  `AuditLog(seal_key=...)`) signs seals and checkpoints while the row key stays on every instance,
  so an attacker who compromises an app instance can forge at most individual unsealed rows — the
  seal chain stays out of reach. Opt-in and additive (no schema change): unset, or equal to
  `mac_key`, is single-key mode and byte-identical to before. When set, sealing and retention's
  checkpoint-writing become a designated sealer role (a host without the seal key does the existing
  loud no-op for sealing, and refuses aligned pruning rather than checkpoint with the wrong key).
- **Key rotation** uses two verify-only, **role-scoped** retired-key archives:
  `FIRM_AUDIT_RETIRED_KEYS` holds retired **row** keys (eligible for row-MAC verification only —
  never a seal, in any mode) and `FIRM_AUDIT_RETIRED_SEAL_KEYS` holds retired **seal** keys
  (eligible for seals *and* rows). A single-key deployment retires its key into the seal archive; a
  split deployment retires row keys into `FIRM_AUDIT_RETIRED_KEYS` and seal keys into
  `FIRM_AUDIT_RETIRED_SEAL_KEYS`. Scoping the archives closes a hole where a row key stolen from an
  app instance could be promoted, once rotated out, into a seal-capable key. (Replaces the earlier
  single flat `FIRM_AUDIT_KEYS` archive, which was role-blind; that variable is gone.)

### Security (pre-release hardening of the unreleased tamper-evidence layer)

- **Anchor tail-truncation is no longer laundered by a checkpoint.** The `verify --anchor` test for
  a legitimately-pruned anchored seal compared a seal *seq* to the checkpoint *floor* (a row id);
  the unit mismatch meant that once any checkpoint existed, deleting the anchored head seal (real
  tail truncation) was accepted as OK. Legitimacy is now judged in seq-space (`seq <= head_seq` with
  a checkpoint present), so the truncation is `TAMPERED`. No anchor-file format change.
- **Forged rows below the checkpoint floor are now detected.** Verify skips recomputation at/below
  the floor, but a checkpoint asserts its pruned range holds zero rows — so verify now asserts the
  pruned region is empty (a bounded probe, every run, not just `--full`). A row inserted at an
  already-pruned id is `TAMPERED` ("row present in a pruned range") instead of invisible.
- **Over-length signed values fail loud at `record()` time.** Under a key, a value longer than its
  column (`VARCHAR(255)` scalars, 65535-byte `TEXT` JSON payloads) would be silently truncated by a
  non-strict MySQL and make the untouched row verify `TAMPERED`; it now raises a `ValueError` before
  the insert. The key-less write path is unchanged (byte-identical to before).
- **`key_id` collisions are a hard error, not a silent identity collapse.** Two *distinct* configured
  keys sharing an 8-hex `key_id` now fail loudly (at startup for the row/seal pair, at verify for a
  colliding retired key) instead of shadowing one another; and whether a two-key split is in force is
  decided by the secret, not the `key_id`, so a collision can no longer downgrade a split to
  single-key.
- **Honest rolling-coverage docs.** The advisory rotation cursor is not MAC-protected, so a
  cursor-pinning attacker can defer a range from non-`--full` runs; docs now state plainly that only
  a periodic `--full` guarantees coverage of every sealed range (the chain, anchor,
  pruned-region-empty, and tail checks remain full every run).
- **Keyring comma contract corrected.** A comma is always an entry delimiter; a secret cannot
  contain one. The parser is fail-closed (never merges two distinct secrets), and the docstring no
  longer over-promises a rejection it cannot make for a well-formed `,label=secret` tail.
- **Deleted sealed rows can no longer be laundered through the late-commit path** (schema + seal-MAC
  recipe change). A seal now records — and signs into its `seal_mac` — the id-gaps it did not cover
  (`firm_audit_seals.gap_ranges`, NULL for a dense range, the common case). Verify's `late_commit`
  branch was previously granted to *any* sealed range where every present row had a valid MAC and the
  count exceeded `row_count`; a database attacker with no key could therefore delete a genuinely
  sealed row and back-fill id-gaps with other valid signed rows (relocation changes only `id`, which
  the row MAC ignored) to pass the deletion off as a benign late commit — then retention pruned and
  checkpointed over it. Verify now only grants `late_commit` when the seal's **covered** (non-gap)
  rows still reproduce the signed `rows_mac`/`row_count` exactly, so a deleted covered row is
  `TAMPERED` and retention refuses the range. A genuine late commit (a validly-signed row landing in
  a recorded gap) is still an amber `WARNING`. Folded into migration `0002` (unreleased); no
  re-signing of existing data.
- **Retention's aligned prune is atomic.** The pre-prune re-verify, the row deletion, and the
  checkpoint write now run in **one transaction** (`snapshot_transaction(write=True)` — `BEGIN
  IMMEDIATE` on SQLite, `SERIALIZABLE` on Postgres/MySQL). A crash mid-prune can no longer leave a
  covering seal with missing rows and no checkpoint (a permanent false `TAMPERED`), and a row
  modified after the check but before the delete can no longer be laundered.
- **`verify` reads a consistent snapshot.** It walks seals then rows across many statements; on
  Postgres/MySQL `READ COMMITTED` a concurrent legitimate prune committing in between made it compare
  stale seals to pruned rows and cry a false `TAMPERED`. Verify now runs in a snapshot transaction
  (`REPEATABLE READ` on Postgres/MySQL, a WAL snapshot on SQLite); it stays read-only.
- **A seal-key-only host never destroys sealed rows.** Retention decided "sealing active" from the
  *row* key, so a two-key sealer/verifier host carrying only `FIRM_AUDIT_SEAL_KEY` fell through to
  the plain age-based delete and removed sealed rows with no checkpoint. Sealing is now active
  whenever any seal exists; a host lacking the seal key that owns the chain refuses the aligned prune
  loudly instead.
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
