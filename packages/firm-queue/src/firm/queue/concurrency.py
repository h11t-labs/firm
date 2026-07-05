"""Concurrency-control spec for a job.

``@job(concurrency={...})`` accepts:

* ``key``         — a callable ``(*args, **kwargs) -> value`` for the variable part of the key
* ``to``          — max simultaneous executions for a key (default 1)
* ``duration``    — seconds before the semaphore/blocked entry expires (failsafe)
* ``group``       — share a key namespace across jobs (defaults to the job's class name)
* ``on_conflict`` — ``"block"`` (queue it) or ``"discard"`` (drop it)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Default concurrency-control period: 3 minutes.
DEFAULT_DURATION = 180.0


@dataclass(frozen=True)
class ConcurrencySpec:
    base_key: str
    limit: int = 1
    duration: float = DEFAULT_DURATION
    on_conflict: str = "block"
    key_fn: Callable[..., Any] | None = None

    def key_for(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        variable = self.key_fn(*args, **kwargs) if self.key_fn is not None else ""
        return f"{self.base_key}/{variable}"

    @classmethod
    def parse(cls, spec: dict[str, Any] | None, *, class_name: str) -> ConcurrencySpec | None:
        if spec is None:
            return None
        on_conflict = spec.get("on_conflict", "block")
        if on_conflict not in ("block", "discard"):
            raise ValueError(f"on_conflict must be 'block' or 'discard', got {on_conflict!r}")
        return cls(
            base_key=spec.get("group") or class_name,
            limit=int(spec.get("to", 1)),
            duration=float(spec.get("duration", DEFAULT_DURATION)),
            on_conflict=on_conflict,
            key_fn=spec.get("key"),
        )
