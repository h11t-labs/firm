"""firm-cache — database-backed cache store for Python.

from firm.cache import Cache

cache = Cache(database_url="sqlite:///cache.db")
cache.set("k", "v")
cache.get("k")
cache.fetch("expensive", lambda: compute())
"""

from __future__ import annotations

from .serialization import JSONCoder, PickleCoder
from .store import Cache

__version__ = "1.0.0"

__all__ = ["Cache", "JSONCoder", "PickleCoder", "__version__"]
