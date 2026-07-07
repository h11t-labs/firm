# Changelog — firm-queue

All notable changes to `firm-queue` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed background jobs, a pure-Python port of Rails'
  [Solid Queue](https://github.com/rails/solid_queue) running on SQLite, PostgreSQL, or
  MySQL/MariaDB.
- Concurrency controls, recurring tasks, retries with configurable backoff, queue pause/resume,
  and job retention.
- Forked and threaded supervisor with heartbeats and crash recovery, plus a `firm-queue` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-queue-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-queue-v0.1.0
