# Changelog — firm-ui

All notable changes to `firm-ui` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The dashboard can be **mounted inside a host application** — on its domain, in its routing,
  behind its own permissions — instead of running as a second process. One adapter per framework,
  each behind its own extra: `firm.ui.contrib.django.dashboard_urls` (`firm-ui[django]`),
  `firm.ui.contrib.flask.blueprint` (`firm-ui[flask]`), and `firm.ui.contrib.fastapi.router`
  (`firm-ui[fastapi]`). Links, form actions, and the preference cookies all carry the mount
  prefix. A mount must state who authenticates it — `authenticator=` (firm-ui checks) or
  `host_auth=True` (the host application does); neither raises `ValueError`, so mounting can never
  silently mean "no auth at all".
- `firm.ui.DashboardApp` — the dashboard with no transport attached: `handle(UIRequest) ->
  UIResponse`, the seam the adapters and the stdlib server both sit on. With `firm.ui.UIRequest`,
  `firm.ui.UIResponse`, and `firm.ui.Headers`, that is all a mount for another framework needs.
- `static_url=` points the stylesheet link at your own static pipeline, and `firm.ui.static_dir()`
  says where the file lives so you can publish it there.

### Changed

- A POST whose `Content-Length` is not a plain number — including two conflicting header lines —
  is answered `400` instead of being read under one of the two lengths. Every transport now agrees
  on how much body there is, or refuses the request.
- The dashboard's read queries moved into the packages that own the data (`firm.queue.queries`,
  `firm.cache.queries`, `firm.channel.queries`, `firm.audit.queries`), where they are now a
  supported API. firm-ui builds on them like any other consumer and keeps only presentation:
  styling, pagination glue, auth, and decoding binary columns for display. The four private
  `firm.ui.*_queries` modules are gone; import from the owning package instead.
- A configured database that cannot be reached or inspected now raises
  `DashboardConnectionError` (a clean CLI error, password masked) instead of silently starting
  the dashboard without that part's tab — a bad password or unreachable host no longer reads as
  "not configured". A reachable database without firm tables still just disables the part.

## [1.0.1] - 2026-07-28

### Changed

- Module pins widened from `~=1.0.0` to `~=1.0`. The old form (`==1.0.*`) meant this package and a
  module's next minor could not be installed together at all; the dashboard now works with any
  1.x module. No behaviour change — the floor is deliberately left where it is, since the dashboard
  needs nothing that shipped in those minors. See `docs/testing-and-contributing.md`
  § Cross-package pins.

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
