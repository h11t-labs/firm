# Changelog — firm-stack

All notable changes to `firm-stack` are documented here.
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

### Added

- Initial release: interim meta-package that installs the firm modules you pick via extras —
  `firm-stack[queue]`, `[cache]`, `[channel]`, `[audit]`, `[ui]`, or `[all]`. Stands in for the
  `firm` meta-package while its PyPI name transfer is pending, and stays as a compatible alias
  afterwards.

[Unreleased]: https://github.com/h11t-labs/firm/compare/firm-stack-v1.0.0...HEAD
[1.0.0]: https://github.com/h11t-labs/firm/compare/firm-stack-v0.1.0...firm-stack-v1.0.0
[0.1.0]: https://github.com/h11t-labs/firm/releases/tag/firm-stack-v0.1.0
