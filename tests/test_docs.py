"""Guard rail: every ```python block in docs/ must parse, and its firm imports must resolve.

This catches doc rot — a renamed/removed symbol or a syntax error in an example fails the suite.
Blocks are checked individually; optional-dependency imports (croniter, fastapi, ...) are skipped
when that dependency isn't installed, so the test is meaningful under any extras combination.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
# Modules used only as illustrative placeholders in multi-file examples.
_PLACEHOLDER_MODS = {"myapp", "jobs", "shared", "app", "worker", "tasks", "pipeline", "models"}


def _blocks() -> list[tuple[str, str]]:
    out = []
    for md in sorted(DOCS.rglob("*.md")):
        text = md.read_text()
        for m in re.finditer(r"```(?:python|py)\n(.*?)```", text, re.S):
            line = text[: m.start()].count("\n") + 1
            out.append((f"{md.relative_to(DOCS.parent)}:{line}", m.group(1)))
    return out


_BLOCKS = _blocks()


def _is_submodule(module: str, name: str) -> bool:
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except ImportError:
        return False


@pytest.mark.parametrize("code", [b[1] for b in _BLOCKS], ids=[b[0] for b in _BLOCKS])
def test_doc_python_block(code: str) -> None:
    tree = ast.parse(code)  # a SyntaxError here fails the test
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PLACEHOLDER_MODS:
                    continue
                try:
                    importlib.import_module(alias.name)
                except ModuleNotFoundError as exc:
                    pytest.skip(f"optional dependency not installed: {exc.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            if node.module.split(".")[0] in _PLACEHOLDER_MODS:
                continue
            try:
                module = importlib.import_module(node.module)
            except ModuleNotFoundError as exc:
                pytest.skip(f"optional dependency not installed: {exc.name}")
                return
            for alias in node.names:
                if alias.name == "*":
                    continue
                assert hasattr(module, alias.name) or _is_submodule(node.module, alias.name), (
                    f"{node.module}.{alias.name} does not exist"
                )


def _load_generator():
    path = ROOT / "scripts" / "gen_llms_full.py"
    spec = importlib.util.spec_from_file_location("gen_llms_full", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llms_full_is_current() -> None:
    """Fail if llms-full.txt is stale relative to the docs (run scripts/gen_llms_full.py)."""
    expected = _load_generator().build_llms_full()
    actual = (ROOT / "llms-full.txt").read_text()
    assert actual == expected, "llms-full.txt is stale — run `python scripts/gen_llms_full.py`"


def test_llms_index_is_current() -> None:
    """Fail if llms.txt is stale — its per-module sections are generated from the zensical nav."""
    expected = _load_generator().build_llms_index()
    actual = (ROOT / "llms.txt").read_text()
    assert actual == expected, "llms.txt is stale — run `python scripts/gen_llms_full.py`"


def test_every_nav_module_appears_in_llms_index() -> None:
    """A module added to the nav must not silently miss the curated index."""
    gen = _load_generator()
    index = gen.build_llms_index()
    for name, _pages in gen._nav_sections(gen._nav(ROOT)):
        if name not in gen._NON_MODULE_SECTIONS:
            assert f"## {name}" in index, f"nav module {name!r} is missing from llms.txt"
