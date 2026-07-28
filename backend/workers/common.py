"""Retry machinery shared by the ingestion and deletion tasks (T-207/T-208, R-38(4)).

Extracted when T-208 added the second task, because the two arq facts R-38(4) records are
not intuitions a second implementation would reproduce by accident — and getting either
wrong is silent:

* **A plain exception is not retried.** Only ``Retry``, ``RetryJob`` and ``CancelledError``
  re-queue a job; anything else marks it finished and drops it. So the backoff is computed
  here and raised as ``Retry(defer=…)``.
* **arq checks ``max_tries`` at the top of ``run_job``**, so an exhausted job is finished
  *without the task being invoked*. A task can therefore never observe its own dead-letter,
  and must detect its final attempt itself — writing ``DEAD_LETTER`` and returning rather
  than raising, because raising there leaves the job ``RUNNING`` forever.

`WORKER_MAX_TRIES` is consequently read by both arq and the tasks, which is why it is one
setting rather than an arq knob plus a task constant.
"""

from __future__ import annotations

import random

from app.config import Settings


def backoff(job_try: int, *, settings: Settings) -> float:
    """Exponential backoff with full jitter, capped.

    arq applies no backoff of its own — ``Retry(defer=…)`` is the only lever — so the shape
    is chosen here. Full jitter (uniform over the whole window rather than a fixed delay
    plus noise) keeps a batch of documents that all failed on the same rate-limited
    embedding call from marching back in lockstep.
    """
    worker = settings.worker
    base = worker.retry_base_seconds
    window = min(base * (2 ** (job_try - 1)), worker.retry_max_seconds)
    return random.uniform(base, window) if window > base else window


def is_final_attempt(job_try: int, *, settings: Settings) -> bool:
    """Whether arq will refuse to deliver this job again after the current attempt."""
    return job_try >= settings.worker.max_tries
