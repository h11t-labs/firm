# Changelog — firm-channel

All notable changes to `firm-channel` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed publish/subscribe, a pure-Python port of Rails'
  [Solid Cable](https://github.com/rails/solid_cable) running on SQLite, PostgreSQL, or
  MySQL/MariaDB.
- Broadcast/subscribe over the database with a polling listener and automatic message trimming,
  plus a `firm-channel` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-channel-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-channel-v0.1.0
