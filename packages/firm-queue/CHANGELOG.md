# Changelog — firm-queue

All notable changes to `firm-queue` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `firm.queue.queries`: the read-query layer the dashboard used to keep to itself, now a supported
  surface for your own dashboards, exporters, and health checks — `state_counts`, `queue_rows`,
  `jobs_by_state`, `job_detail`, `processes`, `recurring`, and `STATES`, plus the single-queue
  reads `queue_names`, `queue_size`, and `queue_latency` (which `queues.all_queues`/`size`/
  `latency` now delegate to). Each takes a SQLAlchemy `Connection` and returns plain dicts. A
  negative `limit`/`offset` or an unknown `state` now raises `ValueError` instead of a bare
  `KeyError` or a backend-specific result. `queue_rows` runs two queries however many queues
  there are, and `job_detail` one.

### Changed

- `firm-core` pin widened from `~=1.0.0` to `~=1.0`, so this package no longer blocks a future
  `firm-core` minor. Ships with the next release of this package. See
  `docs/testing-and-contributing.md` § Cross-package pins.

### Deprecated

- The framework integrations moved from `firm.contrib.*` to **`firm.queue.contrib.*`**, so that
  each module's integrations sit under that module's own path — the same shape
  `firm.cache.contrib.*` already has. `firm.contrib` read like a firm-wide namespace while
  everything in it only ever configured the queue.

  `firm.contrib.flask`, `firm.contrib.fastapi` and `firm.contrib.sqlalchemy` keep working and now
  emit a `DeprecationWarning`; they are removed in 2.0. They re-export the same objects, so `is`
  and `isinstance` still hold across both spellings. Update imports:

  ```python
  from firm.contrib.flask import Firm  # deprecated
  from firm.queue.contrib.flask import Firm  # use this
  ```

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
