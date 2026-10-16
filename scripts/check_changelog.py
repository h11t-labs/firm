"""Check that a package's CHANGELOG.md has a section for its current version.

Usage:
    python scripts/check_changelog.py firm-queue    # one package
    python scripts/check_changelog.py --all         # every package under packages/

Used by .github/workflows/release.yml before anything is built or uploaded: a release tag
for a version without a matching `## [<version>]` heading fails fast. See
docs/testing-and-contributing.md § Releasing.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"


def check(package: str) -> str | None:
    """Return an error message for *package*, or None if its changelog is in order."""
    pyproject = PACKAGES / package / "pyproject.toml"
    if not pyproject.exists():
        return f"{package}: no such package (missing {pyproject.relative_to(ROOT)})"
    with pyproject.open("rb") as f:
        version = tomllib.load(f)["project"]["version"]

    changelog = PACKAGES / package / "CHANGELOG.md"
    if not changelog.exists():
        return f"{package}: missing {changelog.relative_to(ROOT)}"

    heading = f"## [{version}]"
    if not any(line.startswith(heading) for line in changelog.read_text().splitlines()):
        return (
            f"{package} is at {version} but {changelog.relative_to(ROOT)} has no"
            f" `{heading}` section — move the Unreleased entries into"
            f" `{heading} - YYYY-MM-DD` in the version-bump PR"
        )
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    packages = (
        sorted(p.name for p in PACKAGES.iterdir() if (p / "pyproject.toml").exists())
        if argv[0] == "--all"
        else [argv[0]]
    )
    errors = [error for package in packages if (error := check(package))]
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"changelog OK for: {', '.join(packages)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
