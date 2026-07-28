"""Deprecated alias for :mod:`firm.queue.contrib.flask`.

Importing this module warns and re-exports the real one, so ``is`` comparisons and
``isinstance`` keep holding across both paths. See :mod:`firm.contrib` for why it moved.
Removed in 2.0.

The extension class was also renamed ``Firm`` -> ``FirmQueue``; both names resolve here,
whichever one you use.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # resolved lazily at runtime; declared here so type checkers and editors see it
    from firm.queue.contrib.flask import FirmQueue

    Firm = FirmQueue

warnings.warn(
    "firm.contrib.flask has moved to firm.queue.contrib.flask (and Firm is now FirmQueue); "
    "the old path is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name: str) -> Any:
    # Resolved per access rather than bound once at import: a module reload rebuilds the class
    # object, and a snapshot taken here would then be a *different* class from the one the new
    # path hands out — breaking the isinstance guarantee this shim exists to keep.
    if name in ("Firm", "FirmQueue"):
        from firm.queue.contrib import flask as _new

        return _new.FirmQueue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "Firm", "FirmQueue"])


__all__ = ["Firm", "FirmQueue"]
