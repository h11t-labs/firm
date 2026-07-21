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
