"""Worker loop: claim -> run handler (with heartbeats) -> fenced finish.

Run several in parallel; they coordinate only through the jobs table. SIGTERM/SIGINT
finish the current job and exit. SIGKILL loses nothing: the lease expires and another
worker reclaims the job.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path

import psycopg

from app import queue
from app.db import connect, tenant_tx
from app.steps import (Handler, PermanentError, StepContext, TransientError,
                       default_handlers)

log = logging.getLogger(__name__)


class _Heartbeat(threading.Thread):
    """Extends the lease while the handler runs, on its own connection."""

    def __init__(self, url: str, job: queue.Job, lease_seconds: float, every: float):
        super().__init__(daemon=True, name=f"heartbeat-{job.id}")
        self._url, self._job = url, job
        self._lease, self._every = lease_seconds, every
        self._halt = threading.Event()
        self.lost = False

    def run(self) -> None:
        try:
            with connect(self._url) as conn:
                while not self._halt.wait(self._every):
                    if not queue.heartbeat(conn, self._job, self._lease):
                        self.lost = True
                        log.warning("lost lease on job %s", self._job.id)
                        return
        except psycopg.Error:
            # The fence at finish time is the real guarantee; a failed heartbeat
            # just means the lease may expire and the job be redone elsewhere.
            log.exception("heartbeat failed for job %s", self._job.id)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)


class Worker:
    def __init__(
        self,
        url: str,
        *,
        audio_root: str | Path,
        handlers: dict[str, Handler] | None = None,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 15,
        poll_seconds: float = 0.5,
        worker_id: str | None = None,
    ):
        self.url = url
        self.audio_root = Path(audio_root)
        self.handlers = handlers or default_handlers()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.conn = connect(url)

    def close(self) -> None:
        self.conn.close()

    def run_once(self) -> bool:
        """Process at most one job. Returns False when there was nothing to do."""
        job = queue.claim(self.conn, self.worker_id, self.lease_seconds)
        if job is None:
            return False
        self._process(job)
        return True

    def run(self, stop: threading.Event, exit_when_idle: bool = False) -> None:
        log.info("worker %s started", self.worker_id)
        while not stop.is_set():
            if not self.run_once():
                if exit_when_idle:
                    return
                stop.wait(self.poll_seconds)
        log.info("worker %s stopped", self.worker_id)

    def _process(self, job: queue.Job) -> None:
        log.info("job %s %s/%s %s (attempt %s/%s)%s", job.id, job.tenant_id,
                 job.recording_id, job.step, job.attempts, job.max_attempts,
                 " [reclaimed]" if job.reclaimed else "")
        hb = _Heartbeat(self.url, job, self.lease_seconds, self.heartbeat_seconds)
        hb.start()
        try:
            try:
                with tenant_tx(self.conn, job.tenant_id):
                    recording = self.conn.execute(
                        "SELECT * FROM recordings WHERE tenant_id = %s AND id = %s",
                        (job.tenant_id, job.recording_id),
                    ).fetchone()
                handler = self.handlers[job.step]
                result = handler(StepContext(self.conn, job, recording, self.audio_root))
            except PermanentError as exc:
                status = queue.fail_step(self.conn, job, code=exc.code, message=exc.message,
                                         permanent=True,
                                         failed_step=exc.failed_step)
            except TransientError as exc:
                status = queue.fail_step(self.conn, job, code=exc.code, message=exc.message,
                                         permanent=False, retry_after=exc.retry_after,
                                         failed_step=exc.failed_step)
            except Exception as exc:  # noqa: BLE001 - a bug must not drop a recording
                log.exception("unexpected error in %s", job.step)
                status = queue.fail_step(self.conn, job, code="unexpected_error",
                                         message=f"{type(exc).__name__}: {exc}"[:500],
                                         permanent=False)
            else:
                status = queue.complete_step(self.conn, job, persist=result.persist,
                                             duplicate_of=result.duplicate_of)
            log.info("job %s %s/%s -> %s", job.id, job.tenant_id, job.recording_id, status)
        except queue.LeaseLost:
            log.warning("job %s: lease lost, result discarded (another worker owns it)",
                        job.id)
        finally:
            hb.stop()
