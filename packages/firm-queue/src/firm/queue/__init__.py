"""firm-queue — database-backed background jobs for Python.

import firm.queue as bq

bq.configure(database_url="sqlite:///queue.db")

@bq.job(queue="default")
def my_job(x): ...

my_job.enqueue(1)
"""

from __future__ import annotations

from .config import configure, current_runtime
from .job import Job, job

__version__ = "1.0.0"

__all__ = ["Job", "__version__", "configure", "current_runtime", "job"]
