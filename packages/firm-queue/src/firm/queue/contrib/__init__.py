"""Framework integrations that ship with firm-queue.

Nothing here is imported by ``firm.queue`` itself, so importing firm-queue never drags in a web
framework: import the submodule you want.

* :mod:`firm.queue.contrib.flask` — the ``FirmQueue(app)`` extension and ``flask firm-queue worker``
* :mod:`firm.queue.contrib.fastapi` — the ASGI lifespan integration
* :mod:`firm.queue.contrib.sqlalchemy` — ``enqueue_after_commit()`` for a SQLAlchemy session

Each module's path matches the package that ships it, so that a module's integrations are
reachable from that module and nowhere else. The older ``firm.contrib.*`` spellings still work
and warn; see :mod:`firm.contrib`.
"""
