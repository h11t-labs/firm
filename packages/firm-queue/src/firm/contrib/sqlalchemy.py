"""Tie a job enqueue to a SQLAlchemy session's transaction.

``enqueue_after_commit(session, job, *args, **kwargs)`` defers the enqueue until the session
**commits**, and drops it if the session rolls back — so you never enqueue a job for work that
didn't persist. (The enqueue runs in firm's own transaction just after the app's commit;
it is not literally the same transaction, so a crash in the narrow window between the two commits
could still lose the enqueue. For most apps "enqueue iff the request committed" is the goal.)

If an enqueue itself raises after the commit, every queued enqueue is still attempted (one failure
never drops the rest) and the first error then propagates, so the failure is visible rather than
silent.

SQLAlchemy is already a core dependency, so this needs no extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from firm.queue.job import Job

_PENDING = "firm_pending_enqueues"
_WIRED = "firm_after_commit_wired"


def enqueue_after_commit(session: Session, job: Job, *args: Any, **kwargs: Any) -> None:
    """Enqueue ``job`` (a ``@bq.job``) with the given args once ``session`` commits."""
    pending = session.info.setdefault(_PENDING, [])
    pending.append((job, args, kwargs))
    if not session.info.get(_WIRED):
        session.info[_WIRED] = True
        event.listen(session, "after_commit", _flush)
        event.listen(session, "after_rollback", _discard)


def _flush(session: Session) -> None:
    errors = []
    for job, args, kwargs in session.info.pop(_PENDING, []):
        try:
            job.enqueue(*args, **kwargs)
        except Exception as exc:  # attempt the rest before surfacing the failure
            errors.append(exc)
    if errors:
        raise errors[0]


def _discard(session: Session) -> None:
    session.info.pop(_PENDING, None)
