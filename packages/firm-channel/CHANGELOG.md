# Changelog — firm-channel

All notable changes to `firm-channel` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `firm.channel.queries`: the read-query layer the dashboard used to keep to itself, now a
  supported surface for your own dashboards, exporters, and health checks — `channel_stats`,
  `channel_top`, and `channel_recent`, `Connection` in / dicts out. Channel names and payloads come
  back as raw `bytes`, so decoding for display is the caller's decision. A negative `limit`/`offset`
  raises `ValueError`.

### Changed

- `firm-core` pin widened from `~=1.0.0` to `~=1.0`, so this package no longer blocks a future
  `firm-core` minor. Ships with the next release of this package. See
  `docs/testing-and-contributing.md` § Cross-package pins.

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
