# Changelog — firm-core

All notable changes to `firm-core` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

## [0.1.0] - 2026-07-07

### Added

- Initial release: shared internal infrastructure for the firm packages — engine/connection
  handling, per-dialect SQL seams (SQLite, PostgreSQL, MySQL/MariaDB), the interruptible
  poller, the process registry, and configuration plumbing. Not intended for direct use;
  installed automatically by the other `firm-*` packages.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-core-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-core-v0.1.0
