"""Specs for the deprecated ``firm.contrib.*`` aliases.

firm-queue's integrations moved to ``firm.queue.contrib.*`` so that every module's integrations
sit under that module's own path. The old spellings shipped in 1.0.0, so they keep working until
2.0 — and "keep working" has to mean the same objects, not lookalikes, or an ``isinstance``
check in someone's code starts failing across the two paths.

The Flask extension class was renamed in the same release (``Firm`` -> ``FirmQueue``), so the old
path has to keep answering to the old attribute name too.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

# (module, attribute on the old path, attribute on the new path)
MOVED = [
    ("flask", "Firm", "FirmQueue"),
    ("fastapi", "lifespan", "lifespan"),
    ("sqlalchemy", "enqueue_after_commit", "enqueue_after_commit"),
]


@pytest.mark.parametrize(("module", "old_attribute", "new_attribute"), MOVED)
def test_old_path_warns(module: str, old_attribute: str, new_attribute: str) -> None:
    old = importlib.import_module(f"firm.contrib.{module}")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(old)  # the warning fires at import, so re-trigger it
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any(f"firm.queue.contrib.{module}" in m for m in messages), messages
    assert any("2.0" in m for m in messages), messages


@pytest.mark.parametrize(("module", "old_attribute", "new_attribute"), MOVED)
def test_old_path_re_exports_the_same_object(
    module: str, old_attribute: str, new_attribute: str
) -> None:
    """Not a copy: ``isinstance`` and ``is`` have to keep holding across both spellings."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module(f"firm.contrib.{module}")
        new = importlib.import_module(f"firm.queue.contrib.{module}")
        assert getattr(old, old_attribute) is getattr(new, new_attribute)


def test_new_paths_do_not_warn() -> None:
    for module, _, _ in MOVED:
        mod = importlib.import_module(f"firm.queue.contrib.{module}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.reload(mod)
        assert not [w for w in caught if issubclass(w.category, DeprecationWarning)], module


@pytest.mark.parametrize(("module", "old_attribute", "new_attribute"), MOVED)
def test_identity_survives_reloading_the_new_module(
    module: str, old_attribute: str, new_attribute: str
) -> None:
    """A reload rebuilds the class object; the old path must follow, not keep a stale copy.

    Reloads happen for real under Flask's `--reload` dev server and plugin-reload machinery. If
    the shim bound its target once at import, it would hand out the *previous* class afterwards
    and every `isinstance` across the two spellings would start failing — the exact breakage
    these aliases exist to prevent.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module(f"firm.contrib.{module}")
        new = importlib.import_module(f"firm.queue.contrib.{module}")
        importlib.reload(new)
        assert getattr(old, old_attribute) is getattr(new, new_attribute)


def test_old_paths_expose_both_names_to_dir() -> None:
    """`__getattr__`-only names are invisible to dir(); editors and inspect tools need them."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module("firm.contrib.flask")
        new = importlib.import_module("firm.queue.contrib.flask")
    assert {"Firm", "FirmQueue"} <= set(dir(old))
    assert {"Firm", "FirmQueue"} <= set(dir(new))
    assert new.__all__ == ["FirmQueue"]  # but `import *` only ever hands out the new name
