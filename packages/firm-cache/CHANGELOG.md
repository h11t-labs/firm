# Changelog — firm-cache

All notable changes to `firm-cache` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

### Changed

- **Breaking:** the cache table is renamed `firm_entries` → `firm_cache_entries` to match the
  workspace `firm_<module>_<entity>` table convention, and its secondary indexes are renamed to
  match (`index_firm_entries_*` → `index_firm_cache_entries_*`). Migration `0002` renames the table
  and its indexes in place (existing rows preserved). Direct-SQL consumers, least-privilege `GRANT`
  recipes, and anything referencing these names must be updated. A database migrated from 0.1.0
  keeps its original Postgres sequence name `firm_entries_id_seq`.

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed cache store, a pure-Python port of Rails'
  [Solid Cache](https://github.com/rails/solid_cache) running on SQLite, PostgreSQL, or
  MySQL/MariaDB.
- FIFO eviction by age, size, and entry count; pluggable coders; optional at-rest encryption
  (`firm-cache[encryption]`).
- `fetch` with `force`/`skip_nil`, failure-safe reads, and a `firm-cache` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-cache-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-cache-v0.1.0
