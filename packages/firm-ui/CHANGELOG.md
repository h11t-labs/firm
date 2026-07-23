# Changelog — firm-ui

All notable changes to `firm-ui` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
