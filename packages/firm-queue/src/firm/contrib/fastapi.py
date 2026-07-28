"""Deprecated alias for :mod:`firm.queue.contrib.fastapi`.

Importing this module warns and re-exports the real one, so ``is`` comparisons and
``isinstance`` keep holding across both paths. See :mod:`firm.contrib` for why it moved.
Removed in 2.0.
"""

from __future__ import annotations

import warnings

from firm.queue.contrib.fastapi import lifespan

warnings.warn(
    "firm.contrib.fastapi has moved to firm.queue.contrib.fastapi; the old path is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["lifespan"]
