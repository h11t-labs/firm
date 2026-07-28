"""Deprecated alias for :mod:`firm.queue.contrib.flask`.

Importing this module warns and re-exports the real one, so ``is`` comparisons and
``isinstance`` keep holding across both paths. See :mod:`firm.contrib` for why it moved.
Removed in 2.0.

The extension class was also renamed ``Firm`` -> ``FirmQueue``; both names are bound here, so
this module keeps working whichever one you use.
"""

from __future__ import annotations

import warnings

from firm.queue.contrib.flask import FirmQueue

Firm = FirmQueue

warnings.warn(
    "firm.contrib.flask has moved to firm.queue.contrib.flask (and Firm is now FirmQueue); "
    "the old path is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["Firm", "FirmQueue"]
