# Changelog — firm-audit

All notable changes to `firm-audit` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed, append-only audit log (original to firm — no Rails
  counterpart) running on SQLite, PostgreSQL, or MySQL/MariaDB.
- Opt-in retention, `history()` querying, and a `firm-audit` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-audit-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-audit-v0.1.0
