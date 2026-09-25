"""Deprecated location for firm-queue's framework integrations.

These moved to :mod:`firm.queue.contrib` so that every module's integrations sit under that
module's own path — ``firm.queue.contrib.flask`` rather than ``firm.contrib.flask``.
``firm.contrib`` read like a firm-wide namespace while everything in it only ever configured the
queue, and it squats the name a genuinely suite-wide integration would want. The other modules
grow their own ``contrib`` as integrations land for them (the Django work adds one to cache,
channel and audit), which is exactly when queue must not be the odd one out.

The old spellings keep working and emit a ``DeprecationWarning``; they will be removed in 2.0.

    from firm.contrib.flask import Firm             # works, warns
    from firm.queue.contrib.flask import FirmQueue  # same class, no warning

The extension class was renamed ``Firm`` -> ``FirmQueue`` in the same release, for the same
reason: it configures the queue, not the suite.
"""
