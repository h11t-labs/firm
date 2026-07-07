# Changelog — firm-ui

All notable changes to `firm-ui` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are semver-ish
pre-1.0 (breaking changes bump the minor version).

## [Unreleased]

## [0.1.0] - 2026-07-07

### Added

- Initial release: optional web dashboard for firm — watch and operate the queue, cache,
  channel (pub/sub), and audit log in one place. Standard-library HTTP server with Jinja2
  templates.
- Authentication chokepoint with Basic auth, proxy-header, and custom authenticators, plus a
  safe-bind guard for non-loopback interfaces.
- Light / dark / system theme toggle.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-ui-v0.1.0...HEAD
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-ui-v0.1.0
