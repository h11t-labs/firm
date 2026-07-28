"""Deprecated alias for :mod:`firm.queue.contrib.sqlalchemy`.

Importing this module warns and re-exports the real one, so ``is`` comparisons and
``isinstance`` keep holding across both paths. See :mod:`firm.contrib` for why it moved.
Removed in 2.0.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # resolved lazily at runtime; declared here so type checkers and editors see it
    from firm.queue.contrib.sqlalchemy import enqueue_after_commit

warnings.warn(
    "firm.contrib.sqlalchemy has moved to firm.queue.contrib.sqlalchemy; "
    "the old path is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name: str) -> Any:
    # Resolved per access, not bound once at import: a module reload rebuilds the target, and a
    # snapshot taken here would then be a different object from the one the new path hands out.
    if name == "enqueue_after_commit":
        from firm.queue.contrib import sqlalchemy as _new

        return _new.enqueue_after_commit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "enqueue_after_commit"])


__all__ = ["enqueue_after_commit"]
