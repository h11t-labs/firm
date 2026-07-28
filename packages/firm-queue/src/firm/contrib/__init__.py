"""Deprecated location for firm-queue's framework integrations.

These moved to :mod:`firm.queue.contrib` so that every module's integrations sit under that
module's own path — ``firm.queue.contrib.flask`` rather than ``firm.contrib.flask``, the same
shape ``firm.cache.contrib`` already has. ``firm.contrib`` read like a firm-wide namespace while
everything in it only ever configured the queue.

The old spellings keep working and emit a ``DeprecationWarning``; they will be removed in 2.0.

    from firm.contrib.flask import Firm        # works, warns
    from firm.queue.contrib.flask import Firm  # same class, no warning
"""
