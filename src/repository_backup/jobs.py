"""Bounded in-process worker for owner-triggered backup jobs."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

from .catalog import ACTIVE_STATES, Catalog


class Cancelled(RuntimeError):
    pass


Processor = Callable[[dict[str, Any], Callable[[str, dict[str, Any] | None], None], Callable[[], None]], None]


class JobWorker:
    """A fixed-size worker with a bounded queue and explicit state transitions."""

    def __init__(self, catalog: Catalog, processor: Processor, *, workers: int = 1, capacity: int = 32):
        if workers < 1 or capacity < 1:
            raise ValueError("workers and capacity must be positive")
        self.catalog = catalog
        self.processor = processor
        self.queue: queue.Queue[int | None] = queue.Queue(capacity)
        self.threads: list[threading.Thread] = []
        self.worker_count = workers
        self._started = False
        self._submit_lock = threading.RLock()
        self._enqueued: set[int] = set()

    def start(self) -> None:
        if self._started:
            return
        self.catalog.recover_interrupted_jobs(resume=True)
        self._started = True
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._run, name=f"backup-worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)
        # Read durable queued work directly, including rows older than the UI
        # catalog limit. Workers are already running and drain this bounded queue.
        with self.catalog.connection() as db:
            queued = [row[0] for row in db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY id")]
        for job_id in queued:
            with self._submit_lock:
                self._enqueued.add(job_id)
            self.queue.put(job_id)

    def submit(self, repository_id: int, upstream_key: str = "latest") -> dict[str, Any]:
        with self._submit_lock:
            job = self.catalog.create_job(repository_id, upstream_key)
            if job["state"] in ("failed", "cancelled", "stale", "no-release"):
                job = self.catalog.retry_job(job["id"])
            if job["state"] == "queued" and job["id"] not in self._enqueued:
                try:
                    self.queue.put_nowait(job["id"])
                    self._enqueued.add(job["id"])
                except queue.Full as exc:
                    self.catalog.transition_job(job["id"], "failed", expected="queued", error="worker queue is full")
                    raise RuntimeError("worker queue is full") from exc
            return self.catalog.get_job(job["id"])

    def cancel(self, job_id: int) -> dict[str, Any]:
        return self.catalog.request_cancel(job_id)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        for _ in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join(timeout)
        self._started = False
        self.threads.clear()

    def _run(self) -> None:
        while True:
            job_id = self.queue.get()
            try:
                if job_id is None:
                    return
                job = self.catalog.get_job(job_id)
                if not job or job["state"] != "queued":
                    continue

                def check_cancelled() -> None:
                    current = self.catalog.get_job(job_id)
                    if not current or current["cancel_requested"]:
                        raise Cancelled("cancelled by owner")

                current_state = "queued"

                def advance(state: str, progress: dict[str, Any] | None = None) -> None:
                    nonlocal current_state
                    check_cancelled()
                    self.catalog.transition_job(job_id, state, expected=current_state, progress=progress)
                    current_state = state

                try:
                    self.processor(job, advance, check_cancelled)
                    latest = self.catalog.get_job(job_id)
                    if latest and latest["state"] in ACTIVE_STATES:
                        self.catalog.transition_job(job_id, "complete", expected=latest["state"])
                except Cancelled as exc:
                    latest = self.catalog.get_job(job_id)
                    if latest and latest["state"] in ACTIVE_STATES:
                        self.catalog.transition_job(job_id, "cancelled", expected=latest["state"], error=str(exc))
                except Exception as exc:  # the durable record is the error boundary
                    latest = self.catalog.get_job(job_id)
                    if latest and latest["state"] in ACTIVE_STATES:
                        self.catalog.transition_job(job_id, "failed", expected=latest["state"], error=str(exc)[:2000])
            finally:
                with self._submit_lock:
                    self._enqueued.discard(job_id)
                self.queue.task_done()
