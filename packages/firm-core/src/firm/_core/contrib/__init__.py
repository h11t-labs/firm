"""Framework glue that more than one firm module needs.

Framework *integrations* live in the module that owns them (``firm.queue.contrib.flask`` ships with
firm-queue, for instance). What lands here is the small, dependency-free piece two or more
modules would otherwise each grow their own copy of — today: translating a Django ``DATABASES``
entry into a SQLAlchemy URL, which firm-queue and firm-cache both need.

Nothing here imports the framework it is named after; import the submodule you want.
"""

from __future__ import annotations
