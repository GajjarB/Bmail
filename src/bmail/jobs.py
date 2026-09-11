"""Background jobs for the desktop UI.

Ingest and triage take minutes, so an HTTP handler cannot run them inline — the
request would time out and the window would freeze. They run here instead, on a
**single** worker thread, and the UI polls for progress.

One worker, not a pool. The SQLite store and the pipeline are not designed for
concurrent writers, and a "faster" pool would buy contention and corrupted runs.
Queueing is the correct behaviour: two triage runs at once is never what anyone
wanted.
"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

Status = str  # queued | running | done | error | cancelled


@dataclass
class Job:
    id: str
    kind: str
    status: Status = "queued"
    progress: float = 0.0
    step: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- called from inside the worker ---------------------------------------- #

    def update(self, *, progress: float | None = None, step: str | None = None) -> None:
        with self._lock:
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            if step is not None:
                self.step = step

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled(self.kind)

    # -- serialisation ---------------------------------------------------------- #

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "progress": round(self.progress, 4),
                "step": self.step,
                "result": self.result,
                "error": self.error,
                "created_at": _iso(self.created_at),
                "started_at": _iso(self.started_at),
                "finished_at": _iso(self.finished_at),
            }


class JobCancelled(Exception):
    """Raised inside a job when the user asks it to stop."""


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


class JobRunner:
    """A single-worker queue with a bounded history."""

    def __init__(self, *, history: int = 40) -> None:
        self._queue: queue.Queue[tuple[Job, Callable[[Job], dict[str, Any]]]] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._history = history
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._worker = threading.Thread(target=self._run, name="bmail-jobs", daemon=True)
        self._worker.start()

    # -- public ------------------------------------------------------------------ #

    def submit(self, kind: str, work: Callable[[Job], dict[str, Any]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()
        self._queue.put((job, work))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order[-limit:]) if i in self._jobs]

    def active(self) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.status in ("queued", "running"):
                    return job
        return None

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job._cancel.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put(None)  # type: ignore[arg-type]
        self._worker.join(timeout=timeout)

    # -- worker --------------------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stopping.is_set():
            item = self._queue.get()
            if item is None:
                break
            job, work = item
            if job.cancelled:
                self._queue.task_done()
                continue
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            try:
                job.result = work(job) or {}
                job.status = "cancelled" if job.cancelled else "done"
                job.progress = job.progress if job.cancelled else 1.0
            except JobCancelled:
                job.status = "cancelled"
            except Exception as exc:
                # A failed job must never take the worker thread with it, or the
                # UI silently stops accepting work for the rest of the session.
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.result = {"traceback": traceback.format_exc(limit=6)}
            finally:
                job.finished_at = datetime.now(timezone.utc)
                self._queue.task_done()

    def _trim(self) -> None:
        while len(self._order) > self._history:
            stale = self._order.pop(0)
            job = self._jobs.get(stale)
            if job and job.status in ("queued", "running"):
                self._order.insert(0, stale)  # never evict work still in flight
                return
            self._jobs.pop(stale, None)
