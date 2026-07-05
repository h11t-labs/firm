"""Maps a stored ``class_name`` back to the callable that runs it.

We store ``"module.qualname"`` of the decorated function as the ``class_name`` and look the
callable up here at run time. A job enqueued by a deploy that no longer
defines it raises :class:`UnknownJob`, which the worker turns into a failed execution rather
than a crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .job import Job


class UnknownJob(KeyError):
    """Raised when a stored ``class_name`` has no registered callable."""


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def register(self, class_name: str, job: Job) -> None:
        self._jobs[class_name] = job

    def lookup(self, class_name: str) -> Job:
        try:
            return self._jobs[class_name]
        except KeyError:
            raise UnknownJob(class_name) from None

    def __contains__(self, class_name: str) -> bool:
        return class_name in self._jobs

    def all(self) -> dict[str, Job]:
        return dict(self._jobs)


REGISTRY = JobRegistry()
