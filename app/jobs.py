"""Imports run one at a time, in the background on the central server.

Several people importing at once would interleave their writes into the
one database (and the "current state" each import compares against), and
a big workbook would keep a browser waiting until it gave up. So every
import is a Job handed to one worker thread: queued in order, run alone,
its page (/import/jobs/<id>) refreshing until it's done and then showing
the usual result.

On the desktop (one PC) a job runs inline in the request as before - still
one at a time, under the same lock - so nothing changes there.

A job's messages (what an import used to flash()) are kept on the job and
flashed to the person when they see its result; `notify()` is the flash()
that works in both places.
"""

from __future__ import annotations

import collections
import datetime as dt
import itertools
import logging
import threading
import traceback

from flask import flash

log = logging.getLogger(__name__)

KEEP_FINISHED = 200  # finished jobs remembered (their result pages)


class Job:
    def __init__(self, job_id: int, kind: str, label: str, owner: str, fn):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.owner = owner
        self.fn = fn
        self.status = "queued"  # queued -> running -> done / error
        self.created = dt.datetime.now()
        self.started: dt.datetime | None = None
        self.finished: dt.datetime | None = None
        self.messages: list[tuple[str, str]] = []
        self.outcome: dict = {}
        self.error = ""
        self.delivered = False  # its messages already flashed to the owner

    @property
    def done(self) -> bool:
        return self.status in ("done", "error")


_current = threading.local()


def notify(message: str, category: str = "info") -> None:
    """flash() for code that may run inside a job (no request there)."""
    job = getattr(_current, "job", None)
    if job is not None:
        job.messages.append((category, message))
    else:
        flash(message, category)


class JobRunner:
    def __init__(self, name: str = "Import"):
        self.name = name
        self._ids = itertools.count(1)
        self._jobs: "collections.OrderedDict[int, Job]" = collections.OrderedDict()
        self._queue: collections.deque[Job] = collections.deque()
        self._mutex = threading.Lock()
        self._wake = threading.Condition(self._mutex)
        self._run_lock = threading.Lock()  # one job at a time, inline or not
        self._worker: threading.Thread | None = None
        self._app = None

    # --- submitting ---------------------------------------------------------------

    def submit(self, app, kind: str, label: str, owner: str, fn, background: bool) -> Job:
        """fn() -> outcome dict, run alone (inline now, or by the worker)."""
        with self._mutex:
            job = Job(next(self._ids), kind, label, owner, fn)
            self._jobs[job.id] = job
            self._trim()
        if not background:
            self._run(job)
            return job
        with self._wake:
            self._app = app
            self._queue.append(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name=f"{self.name.lower()}-jobs", daemon=True)
                self._worker.start()
            self._wake.notify()
        log.info("%s job %s queued: %s (%s)", self.name, job.id, label, owner)
        return job

    def get(self, job_id: int) -> Job | None:
        with self._mutex:
            return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        """Running first, then the queue in order."""
        with self._mutex:
            jobs = [j for j in self._jobs.values() if not j.done]
        return sorted(jobs, key=lambda j: (j.status != "running", j.id))

    def latest_done(self, owner: str) -> Job | None:
        """This person's newest finished job (its result page)."""
        with self._mutex:
            mine = [j for j in self._jobs.values() if j.owner == owner and j.done]
        return mine[-1] if mine else None

    def ahead_of(self, job: Job) -> int:
        """How many jobs run before this one (0 = it's running / next)."""
        active = self.active()
        return active.index(job) if job in active else 0

    def reset(self) -> None:
        with self._mutex:
            self._jobs.clear()
            self._queue.clear()

    # --- running ------------------------------------------------------------------

    def _run(self, job: Job) -> None:
        with self._run_lock:
            job.status = "running"
            job.started = dt.datetime.now()
            _current.job = job
            try:
                job.outcome = job.fn() or {}
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - shown on the job's page, full trace in the log
                job.error = f"{type(e).__name__}: {e}"
                job.status = "error"
                log.error("%s job %s (%s) failed:\n%s", self.name, job.id, job.label, traceback.format_exc())
            finally:
                _current.job = None
                job.finished = dt.datetime.now()
                job.fn = None  # drop what it held (file lists...)
        log.info("%s job %s %s in %.1fs: %s", self.name, job.id, job.status,
                 (job.finished - job.started).total_seconds(), job.label)

    def _work(self) -> None:
        while True:
            with self._wake:
                while not self._queue:
                    self._wake.wait()
                job = self._queue.popleft()
                app = self._app
            if app is not None:
                with app.app_context():
                    self._run(job)
            else:
                self._run(job)

    def _trim(self) -> None:
        finished = [j.id for j in self._jobs.values() if j.done]
        for job_id in finished[:max(0, len(finished) - KEEP_FINISHED)]:
            del self._jobs[job_id]


runner = JobRunner("Import")
# CUCM scans: one at a time too - CUCM's RisPort API allows only a few
# requests a minute, so two people auto-split scanning at once would both
# hit its limit. Kept apart from imports, so a scan never waits for one.
cucm_runner = JobRunner("CUCM scan")
