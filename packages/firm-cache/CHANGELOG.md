# Changelog — firm-cache

All notable changes to `firm-cache` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

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
