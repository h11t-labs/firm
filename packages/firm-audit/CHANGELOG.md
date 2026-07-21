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
