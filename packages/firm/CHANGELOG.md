# Changelog — firm

All notable changes to the `firm` meta-package are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-23

First stable release: the PyPI classifier moves to **Production/Stable** and the
inter-package pins to `~=1.0.0`.

### Changed

- Extras now pin the firm modules at `~=1.0.0` for the coordinated 1.0 release. No functional
  changes to the meta-package itself.

## [0.1.0] - 2026-07-07

> Not yet on PyPI: publication awaits the pending
> [PEP 541 name transfer](https://github.com/pypi/support/issues/11384). Install
> [`firm-stack`](https://pypi.org/project/firm-stack/) in the meantime — it provides the same
> extras.

### Added

- Initial version: meta-package that installs the firm modules you pick via extras —
  `firm[queue]`, `[cache]`, `[channel]`, `[audit]`, `[ui]`, or `[all]`.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-v0.1.0...firm-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-v0.1.0
