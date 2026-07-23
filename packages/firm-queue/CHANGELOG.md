# Changelog — firm-queue

All notable changes to `firm-queue` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-23

First stable release: the PyPI classifier moves to **Production/Stable** and the
inter-package pins to `~=1.0.0`.

### Changed

- **Breaking:** every firm-queue table is renamed to the workspace `firm_<module>_<entity>`
  convention — `firm_jobs` → `firm_queue_jobs`, `firm_ready_executions` →
  `firm_queue_ready_executions`, `firm_claimed_executions` → `firm_queue_claimed_executions`,
  `firm_scheduled_executions` → `firm_queue_scheduled_executions`, `firm_blocked_executions` →
  `firm_queue_blocked_executions`, `firm_failed_executions` → `firm_queue_failed_executions`,
  `firm_recurring_executions` → `firm_queue_recurring_executions`, `firm_recurring_tasks` →
  `firm_queue_recurring_tasks`, `firm_pauses` → `firm_queue_pauses`, `firm_semaphores` →
  `firm_queue_semaphores`, and the shared `firm_processes` table → `firm_queue_processes`. Every
  secondary index is renamed to match (`index_firm_jobs_*` → `index_firm_queue_jobs_*`, etc.).
  Migration `0002` renames the tables and indexes in place (existing rows preserved). Direct-SQL
  consumers, least-privilege `GRANT` recipes, and anything referencing these table or index names
  must be updated. A database migrated from 0.1.0 keeps its original Postgres sequence names
  (e.g. `firm_jobs_id_seq`).

## [0.1.0] - 2026-07-07

### Added

- Initial release: database-backed background jobs, a pure-Python port of Rails'
  [Solid Queue](https://github.com/rails/solid_queue) running on SQLite, PostgreSQL, or
  MySQL/MariaDB.
- Concurrency controls, recurring tasks, retries with configurable backoff, queue pause/resume,
  and job retention.
- Forked and threaded supervisor with heartbeats and crash recovery, plus a `firm-queue` CLI.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-queue-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-queue-v0.1.0...firm-queue-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-queue-v0.1.0
