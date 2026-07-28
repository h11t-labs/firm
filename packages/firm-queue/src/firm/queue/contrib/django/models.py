"""Deliberately empty: firm defines no Django models.

firm's tables are SQLAlchemy Core and live outside Django's migration graph — ``makemigrations``
never sees them, never proposes a migration for them and never drops them.

This module exists for one reason: ``emit_post_migrate_signal`` skips every app config whose
``models_module`` is ``None``. Without a models module, ``post_migrate`` would never fire with
this app as its sender and ``manage.py migrate`` would not create firm's schema. Deleting this
file silently breaks that.
"""

from __future__ import annotations
