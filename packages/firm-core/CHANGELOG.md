# Changelog — firm-core

All notable changes to `firm-core` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-23

First stable release: the PyPI classifier moves to **Production/Stable** and the
inter-package pins to `~=1.0.0`.

### Added

- Coordination-locking seams for firm-audit's tamper-evidence layer: `snapshot_transaction`
  (snapshot reads, `BEGIN IMMEDIATE` write lock) and a `with_row_lock` dialect helper
  (`FOR UPDATE` on PostgreSQL/MySQL, a no-op on SQLite paired with `BEGIN IMMEDIATE`).

## [0.1.0] - 2026-07-07

### Added

- Initial release: shared internal infrastructure for the firm packages — engine/connection
  handling, per-dialect SQL seams (SQLite, PostgreSQL, MySQL/MariaDB), the interruptible
  poller, the process registry, and configuration plumbing. Not intended for direct use;
  installed automatically by the other `firm-*` packages.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-core-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-core-v0.1.0...firm-core-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-core-v0.1.0
