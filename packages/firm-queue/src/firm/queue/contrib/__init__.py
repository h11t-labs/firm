"""Framework integrations that ship with firm-queue.

Nothing here is imported by ``firm.queue`` itself, so importing firm-queue never drags in a web
framework: import the submodule you want.

* :mod:`firm.queue.contrib.flask` — the ``Firm(app)`` extension and ``flask firm worker``
* :mod:`firm.queue.contrib.fastapi` — the ASGI lifespan integration
* :mod:`firm.queue.contrib.sqlalchemy` — ``enqueue_after_commit()`` for a SQLAlchemy session

Each module's path matches the package that ships it, the same shape ``firm.cache.contrib`` has.
The older ``firm.contrib.*`` spellings still work and warn; see :mod:`firm.contrib`.
"""
