"""Specs for the deprecated ``firm.contrib.*`` aliases.

firm-queue's integrations moved to ``firm.queue.contrib.*`` so that every module's integrations
sit under that module's own path. The old spellings shipped in 1.0.0, so they keep working until
2.0 — and "keep working" has to mean the same objects, not lookalikes, or an ``isinstance``
check in someone's code starts failing across the two paths.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

MOVED = [
    ("flask", "Firm"),
    ("fastapi", "lifespan"),
    ("sqlalchemy", "enqueue_after_commit"),
]


@pytest.mark.parametrize(("module", "attribute"), MOVED)
def test_old_path_warns(module: str, attribute: str) -> None:
    old = importlib.import_module(f"firm.contrib.{module}")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(old)  # the warning fires at import, so re-trigger it
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any(f"firm.queue.contrib.{module}" in m for m in messages), messages
    assert any("2.0" in m for m in messages), messages


@pytest.mark.parametrize(("module", "attribute"), MOVED)
def test_old_path_re_exports_the_same_object(module: str, attribute: str) -> None:
    """Not a copy: ``isinstance`` and ``is`` have to keep holding across both spellings."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module(f"firm.contrib.{module}")
    new = importlib.import_module(f"firm.queue.contrib.{module}")
    assert getattr(old, attribute) is getattr(new, attribute)


def test_new_paths_do_not_warn() -> None:
    for module, _ in MOVED:
        mod = importlib.import_module(f"firm.queue.contrib.{module}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.reload(mod)
        assert not [w for w in caught if issubclass(w.category, DeprecationWarning)], module


def test_django_has_no_deprecated_alias() -> None:
    """The Django integration was never released under `firm.contrib`, so there is nothing to
    stay compatible with — an alias would only invite people onto a path due to be removed."""
    with pytest.raises(ImportError):
        importlib.import_module("firm.contrib.django")
