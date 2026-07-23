"""Internal infrastructure shared across the firm packages (ships as ``firm-core``).

This subpackage is the local equivalent of what ActiveRecord provides for the Ruby
gems: engine/connection handling, dialect-specific locking, a reusable interruptible
poller, process registration, and configuration. Every firm module (queue, cache,
channel, audit, ui) depends on it; nothing here imports those modules.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
