# Changelog — firm-core

All notable changes to `firm-core` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `firm._core.contrib.django.sqlalchemy_url_for()` — turns one Django `DATABASES` entry into a
  SQLAlchemy URL firm can open, so every module's Django integration shares one mapping instead
  of each carrying its own. Handles unix sockets, credentials needing percent-encoding, and the
  `OPTIONS` keys Django consumes itself. Needs no Django import.
- `firm._core.contrib.django_handle` — the shared machinery behind the Django handles in
  firm-channel and firm-audit: settings-block validation, URL derivation, and a `LazyHandle` that
  builds its object on first access, rebinds when the database changes, and reuses firm-queue's
  engine when it is pointed at the same one.

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
