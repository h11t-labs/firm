#!/usr/bin/env python3
"""Mechanically verify firm's upstream test-parity policy.

firm's port promise is that its test suite is a *superset* of the Rails Solid gems' tests (minus
documented divergences). ``tests/parity_inventory.toml`` is the machine-readable record of that:
every upstream Solid test file firm tracks, mapped to the firm test(s) that port it or an explicit
divergence. This script keeps the inventory and the tests honest so the policy can't rot silently:

  * drift guard   — every upstream ``*.rb`` a firm test cites must be listed in the inventory
  * rot guard     — every ``ported_by`` file must exist and actually cite its upstream
  * dead-entry    — every ``ported`` upstream must be cited by at least one test
  * placement     — a ``[[<module>]]`` entry's ``ported_by`` must live under ``tests/<module>/``
  * honesty guard — every ``diverged`` entry must carry a reason

Run ``python scripts/check_parity.py`` (also wired into CI + pre-commit). Exits non-zero on any
violation with a per-problem report. See docs/testing-and-contributing.md for the contract.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
INVENTORY = TESTS / "parity_inventory.toml"
MODULES = ("queue", "cache", "channel")

# Upstream citations are Ruby filenames in comments, e.g. ``ready_execution_test.rb``. A leading
# glob (``*_adapter_test.rb``) is captured as ``_adapter_test.rb``.
_RB = re.compile(r"[a-z_]+\.rb")


def citations_by_test() -> dict[str, set[str]]:
    """Map each firm test file (repo-relative) to the set of upstream ``*.rb`` files it cites."""
    out: dict[str, set[str]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        cites = set(_RB.findall(path.read_text(encoding="utf-8")))
        if cites:
            out[str(path.relative_to(ROOT))] = cites
    return out


def main() -> int:
    if not INVENTORY.exists():
        print(f"error: inventory not found at {INVENTORY.relative_to(ROOT)}", file=sys.stderr)
        return 1

    inventory = tomllib.loads(INVENTORY.read_text(encoding="utf-8"))
    cites_by_test = citations_by_test()
    cited_files = {rb for cites in cites_by_test.values() for rb in cites}

    problems: list[str] = []
    inventory_files: set[str] = set()  # every entry's upstream file part
    ported_files: set[str] = set()  # upstream files claimed as ported

    for module in MODULES:
        for entry in inventory.get(module, []):
            upstream = entry.get("upstream")
            if not upstream:
                problems.append(f"[{module}] an entry has no `upstream` key: {entry!r}")
                continue
            file_part = upstream.split("::", 1)[0]
            inventory_files.add(file_part)

            ported_by = entry.get("ported_by")
            if ported_by:
                ported_files.add(file_part)
                for rel in ported_by:
                    if not (ROOT / rel).exists():
                        problems.append(f"[{module}] {upstream}: ported_by file is missing: {rel}")
                        continue
                    if not rel.startswith(f"tests/{module}/"):
                        problems.append(
                            f"[{module}] {upstream}: ported_by {rel} is not under tests/{module}/"
                        )
                    if file_part not in cites_by_test.get(rel, set()):
                        problems.append(
                            f"[{module}] {upstream}: {rel} is listed as porting it "
                            f"but does not cite `{file_part}`"
                        )
            elif entry.get("diverged"):
                if not str(entry.get("reason", "")).strip():
                    problems.append(f"[{module}] {upstream}: diverged=true but no `reason` given")
            else:
                problems.append(
                    f"[{module}] {upstream}: entry is neither ported (`ported_by`) "
                    "nor a documented divergence (`diverged` + `reason`)"
                )

    # Drift guard: every upstream a test cites must be inventoried.
    for rb in sorted(cited_files - inventory_files):
        citing = sorted(t for t, c in cites_by_test.items() if rb in c)
        problems.append(f"upstream `{rb}` is cited by {citing} but is missing from the inventory")

    # Dead-entry guard: every ported upstream must actually be cited somewhere.
    for rb in sorted(ported_files - cited_files):
        problems.append(f"inventory lists `{rb}` as ported but no test cites it")

    if problems:
        print("Parity inventory check FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"\n{len(problems)} problem(s). Reconcile {INVENTORY.relative_to(ROOT)} with the "
            "parity tests (see docs/testing-and-contributing.md).",
            file=sys.stderr,
        )
        return 1

    diverged = sum(
        1 for module in MODULES for entry in inventory.get(module, []) if entry.get("diverged")
    )
    print(
        f"parity inventory OK: {len(ported_files)} upstream files ported across "
        f"{len(cites_by_test)} test files, {diverged} documented divergence(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
