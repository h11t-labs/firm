# Changelog — firm-ui

All notable changes to `firm-ui` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Post-action notices.** Write actions (pause/resume, retry, discard, retry-all, clear cache,
  trim channel) now redirect back with a flash bar, so a refused or no-op action is visibly
  distinct from a success — a refused retry reads "Nothing to retry", a bulk clear reports its
  count, and "cleared 0 entries" shows as a warning rather than a green success. The notice token
  is whitelisted server-side (no reflected free text) and counts are integers.

### Fixed

- **Overview N+1.** The queue overview issued two `SELECT`s per queue inside a Python loop on a
  page that auto-refreshes; it now runs a single grouped query over `firm_queue_ready_executions`,
  merging paused queues back in.
- **Busiest-channels pager mismatch.** The `channels` stat counted `DISTINCT channel_hash` while
  the table groups by raw `channel`; both now count `DISTINCT channel`, so the pager total can't
  promise a row the table can't show (index-friendly via the raw-channel index).
- **Giant route ids.** A `/job/<id>` (or `/audit/<id>`) path segment past CPython's int-string
  limit (>4300 digits) raised `ValueError` and surfaced as a 500; it is now a clean 404.

## [1.0.0] - 2026-07-23

First stable release: the PyPI classifier moves to **Production/Stable** and the
inter-package pins to `~=1.0.0`.

### Added

- Audit **integrity panel** and per-row integrity status in the audit table, backed by the
  firm-audit verifier's canonical status row. Hardened against spoofed, oversized, or
  deeply-nested status input, and honest about rows a truncated verify run could not vouch for.

## [0.1.0] - 2026-07-07

### Added

- Initial release: optional web dashboard for firm — watch and operate the queue, cache,
  channel (pub/sub), and audit log in one place. Standard-library HTTP server with Jinja2
  templates.
- Authentication chokepoint with Basic auth, proxy-header, and custom authenticators, plus a
  safe-bind guard for non-loopback interfaces.
- Light / dark / system theme toggle.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-ui-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-ui-v0.1.0...firm-ui-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-ui-v0.1.0
