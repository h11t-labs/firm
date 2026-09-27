"""Worker — claim ready jobs and run them on a thread pool.

:func:`run_ready` is the synchronous one-shot (claim a batch, run it inline) behind the
``drain`` command and used directly by tests. :class:`Worker` is the long-running poller:
each cycle claims up to
``threads`` jobs and runs them concurrently on a :class:`~concurrent.futures.ThreadPoolExecutor`.
True multi-core parallelism comes from running several worker *processes* (the supervisor);
threads here parallelize I/O-bound jobs (and CPU-bound ones under a free-threaded build).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor

from .._core.config import Runtime
from .._core.poller import InterruptiblePoller
from .claim import claim_ready
from .hooks import HOOKS
from .results import FinalizeError, execute_claimed


def run_ready(
    runtime: Runtime,
    queues: Sequence[str] = ("*",),
    limit: int = 10,
    process_id: int | None = None,
) -> int:
    """Claim up to ``limit`` ready jobs and run them inline; return how many were processed."""
    claimed = claim_ready(runtime.engine, runtime.dialect, list(queues), limit, process_id)
    for job_id in claimed:
        execute_claimed(runtime, job_id, process_id)
    return len(claimed)


class Worker(InterruptiblePoller):
    def __init__(
        self,
        runtime: Runtime,
        queues: Sequence[str] = ("*",),
        threads: int = 3,
        poll_interval: float = 0.1,
        process_id: int | None = None,
    ) -> None:
        super().__init__(
            poll_interval,
            name="worker",
            idle_interval=max(poll_interval * 10, 1.0),
            on_error=HOOKS.fire_error,
        )
        self.runtime = runtime
        self.queues = list(queues)
        self.threads = threads
        self.process_id = process_id
        self._pool: ThreadPoolExecutor | None = None
        # Outcomes that could not be recorded within execute_claimed's own retry budget (their
        # claim rows are still ours). Only touched on the poller thread: poll() and on_stop()
        # both run there, so no lock.
        self._pending: list[FinalizeError] = []

    def on_start(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="bq-job")

    def on_stop(self) -> None:
        # A last attempt at outcomes still pending, before the process deregisters and the
        # absent-row sweep re-readies their claims (a duplicate run). One try, no backoff:
        # stop() joins this thread with a 5s timeout.
        pending, self._pending = self._pending, []
        for err in pending:
            try:
                err.finalize()
            except Exception as exc:
                HOOKS.fire_error(exc)
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def poll(self) -> int:
        assert self._pool is not None
        retried = 0
        if self._pending:
            # Record pending outcomes *before* claiming: the database is presumably back, and
            # releasing their semaphore slots first lets blocked siblings into this very claim.
            pending, self._pending = self._pending, []
            self._collect(self._pool.submit(err.retry) for err in pending)
            retried = len(pending)
        claimed = claim_ready(
            self.runtime.engine, self.runtime.dialect, self.queues, self.threads, self.process_id
        )
        if not claimed:
            return retried
        self._collect(
            self._pool.submit(execute_claimed, self.runtime, job_id, self.process_id)
            for job_id in claimed
        )
        return retried + len(claimed)

    def _collect(self, futures: Iterable[Future[object]]) -> None:
        for future in list(futures):
            try:
                future.result()
            except FinalizeError as exc:
                # The body ran but its outcome is not recorded; the claim is still ours, so keep
                # it for the next poll rather than leave it stranded under a live process.
                # Reported once per exhausted budget, so operators see the outage.
                self._pending.append(exc)
                HOOKS.fire_error(exc)
            except Exception as exc:
                # Surface every infrastructure failure and keep retrieving the remaining
                # futures — aborting on the first would drop the siblings' exceptions.
                HOOKS.fire_error(exc)
