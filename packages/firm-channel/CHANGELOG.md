# Changelog — firm-channel

All notable changes to `firm-channel` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Channel names longer than the 1024-byte `channel` column are now truncated with a hash suffix
  (mirroring the cache's key normalization) so they stay unique and fit. Previously an over-long
  name raised a MySQL "Data too long" error or was silently truncated, while SQLite/Postgres
  accepted it — an inconsistency across backends.

### Added

- `firm-channel stats` now also reports an estimated total payload size (`SUM(length(payload))`),
  matching `firm-cache stats`'s count-plus-size output.

### Changed

- `firm-channel trim` builds its one-shot `Channel` with `auto_trim=False`, so the command no
  longer spins up a background trimmer thread pool it never uses (matching `firm-cache`'s one-shot
  commands, which pass `auto_expire=False`).

## [1.0.0] - 2026-07-23

First stable release: the PyPI classifier moves to **Production/Stable** and the
inter-package pins to `~=1.0.0`.

### Changed

- **Breaking:** the messages table is renamed `firm_messages` → `firm_channel_messages` to match
  the workspace `firm_<module>_<entity>` table convention, and its secondary indexes are renamed to
  match (`index_firm_messages_*` → `index_firm_channel_messages_*`). Migration `0002` renames the
  table and its indexes in place (existing rows preserved). Direct-SQL consumers, least-privilege
  `GRANT` recipes, and anything referencing these names must be updated. A database migrated from
  0.1.0 keeps its original Postgres sequence name `firm_messages_id_seq`.

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed publish/subscribe, a pure-Python port of Rails'
  [Solid Cable](https://github.com/rails/solid_cable) running on SQLite, PostgreSQL, or
  MySQL/MariaDB.
- Broadcast/subscribe over the database with a polling listener and automatic message trimming,
  plus a `firm-channel` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-channel-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-channel-v0.1.0...firm-channel-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-channel-v0.1.0
