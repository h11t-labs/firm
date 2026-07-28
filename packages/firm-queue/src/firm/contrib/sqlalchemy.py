"""Deprecated alias for :mod:`firm.queue.contrib.sqlalchemy`.

Importing this module warns and re-exports the real one, so ``is`` comparisons and
``isinstance`` keep holding across both paths. See :mod:`firm.contrib` for why it moved.
Removed in 2.0.
"""

from __future__ import annotations

import warnings

from firm.queue.contrib.sqlalchemy import enqueue_after_commit

warnings.warn(
    "firm.contrib.sqlalchemy has moved to firm.queue.contrib.sqlalchemy; "
    "the old path is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["enqueue_after_commit"]
