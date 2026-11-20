"""PostgreSQL work queue + recording state machine.

    PENDING -> VALIDATING -> TRANSCRIBING -> MASKING -> PROCESSING_CRM -> COMPLETED
                    |             |             |              |
                    +-------------+-------------+--------------+--> FAILED (failed_step, error_*)
                    +--> DUPLICATE (duplicate_of)

Invariants:
  * The job row and the recording row change in the SAME transaction, always.
  * A job is claimed with FOR UPDATE SKIP LOCKED, then held by a lease (locked_until).
    No transaction stays open while a step runs (speech-to-text can take minutes).
  * A running job whose lease expired (worker SIGKILLed / frozen) is claimable again.
  * Every write after the claim is fenced by lease_token: a worker that lost its lease
    cannot finish, fail or reschedule the job (LeaseLost), so a zombie cannot overwrite
    the work of the worker that took over.
  * attempts counts claims of the current step, so a job that kills its worker every
    time ends as FAILED(worker_lost) instead of looping forever.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

from app.db import set_tenant

STEPS = ("VALIDATION", "TRANSCRIPTION", "MASKING", "CRM")
STATUS_FOR_STEP = {
    "VALIDATION": "VALIDATING",
    "TRANSCRIPTION": "TRANSCRIBING",
    "MASKING": "MASKING",
    "CRM": "PROCESSING_CRM",
}
MAX_ERROR_LEN = 2000


class LeaseLost(Exception):
    """This worker no longer owns the job; its result must be discarded."""


@dataclass(frozen=True)
class Job:
    id: int
    tenant_id: str
    recording_id: str
    step: str
    attempts: int          # including the current claim
    max_attempts: int
    lease_token: uuid.UUID
    worker_id: str
    reclaimed: bool        # previous holder's lease had expired


def next_step(step: str) -> str | None:
    i = STEPS.index(step)
    return STEPS[i + 1] if i + 1 < len(STEPS) else None


# Rate-limit waits per step that do not count as attempts (see fail_step). 30 waits of a
# typical Groq Retry-After (seconds to a minute) cover a free-tier burst many times over.
MAX_THROTTLE_WAITS = 30


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    return min(cap, base * 2 ** max(0, attempt - 1)) + random.uniform(0, 1)


def _event(conn, tenant_id, recording_id, event, *, step=None, from_status=None,
           to_status=None, detail=None, worker=None) -> None:
    conn.execute(
        "INSERT INTO recording_events (tenant_id, recording_id, event, step, from_status,"
        " to_status, detail, worker) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, recording_id, event, step, from_status, to_status,
         detail[:MAX_ERROR_LEN] if detail else None, worker),
    )


def _lock_recording(conn, tenant_id: str, recording_id: str) -> dict:
    row = conn.execute(
        "SELECT status FROM recordings WHERE tenant_id = %s AND id = %s FOR UPDATE",
        (tenant_id, recording_id),
    ).fetchone()
    if row is None:  # cannot happen: FK + RLS context is the job's own tenant
        raise RuntimeError(f"recording {tenant_id}/{recording_id} not visible")
    return row


# ---------------------------------------------------------------------------- claim

_CLAIM_SQL = """
SELECT id, tenant_id, recording_id, step, state, attempts, max_attempts, locked_by
FROM jobs
WHERE (state = 'queued'  AND run_at <= now())
   OR (state = 'running' AND locked_until < now())
ORDER BY run_at, id
LIMIT 1
FOR UPDATE SKIP LOCKED
"""


def claim(conn: psycopg.Connection, worker_id: str, lease_seconds: float) -> Job | None:
    """Claim the next runnable job (or an expired lease). Returns None if idle."""
    while True:
        with conn.transaction():
            row = conn.execute(_CLAIM_SQL).fetchone()
            if row is None:
                return None
            reclaimed = row["state"] == "running"
            set_tenant(conn, row["tenant_id"])

            if reclaimed and row["attempts"] >= row["max_attempts"]:
                _mark_failed(
                    conn, row["id"], row["tenant_id"], row["recording_id"], row["step"],
                    "worker_lost",
                    f"worker {row['locked_by']} stopped without finishing (lease expired);"
                    f" {row['attempts']} of {row['max_attempts']} attempts used",
                    worker_id,
                )
                continue  # commit, then look for the next job

            token = uuid.uuid4()
            conn.execute(
                "UPDATE jobs SET state = 'running', attempts = attempts + 1, locked_by = %s,"
                " locked_until = now() + make_interval(secs => %s), lease_token = %s,"
                " updated_at = now() WHERE id = %s",
                (worker_id, lease_seconds, token, row["id"]),
            )
            rec = _lock_recording(conn, row["tenant_id"], row["recording_id"])
            to_status = STATUS_FOR_STEP[row["step"]]
            is_retry = row["attempts"] > 0
            conn.execute(
                "UPDATE recordings SET status = %s, current_step = %s,"
                " retry_count = retry_count + %s, updated_at = now()"
                " WHERE tenant_id = %s AND id = %s",
                (to_status, row["step"], 1 if is_retry else 0,
                 row["tenant_id"], row["recording_id"]),
            )
            _event(
                conn, row["tenant_id"], row["recording_id"],
                "lease_reclaimed" if reclaimed else "claimed",
                step=row["step"], from_status=rec["status"], to_status=to_status,
                detail=(f"previous holder {row['locked_by']} lost its lease; " if reclaimed
                        else "") + f"attempt {row['attempts'] + 1}/{row['max_attempts']}",
                worker=worker_id,
            )
            return Job(
                id=row["id"], tenant_id=row["tenant_id"], recording_id=row["recording_id"],
                step=row["step"], attempts=row["attempts"] + 1,
                max_attempts=row["max_attempts"], lease_token=token, worker_id=worker_id,
                reclaimed=reclaimed,
            )


def heartbeat(conn: psycopg.Connection, job: Job, lease_seconds: float) -> bool:
    """Extend the lease. False means it was lost (someone reclaimed it)."""
    cur = conn.execute(
        "UPDATE jobs SET locked_until = now() + make_interval(secs => %s), updated_at = now()"
        " WHERE id = %s AND lease_token = %s AND state = 'running'",
        (lease_seconds, job.id, job.lease_token),
    )
    return cur.rowcount == 1


# ------------------------------------------------------------------ fenced finishing

@contextmanager
def owned(conn: psycopg.Connection, job: Job) -> Iterator[psycopg.Connection]:
    """A transaction that only proceeds while this worker still holds the job's lease
    (fencing). Steps use it for intermediate writes, e.g. committing a CRM attempt
    before sending it."""
    with conn.transaction():
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE id = %s AND lease_token = %s AND state = 'running'"
            " FOR UPDATE",
            (job.id, job.lease_token),
        ).fetchone()
        if row is None:
            raise LeaseLost(f"job {job.id} ({job.tenant_id}/{job.recording_id}) is no longer ours")
        set_tenant(conn, job.tenant_id)
        yield conn


_CLEAR_LEASE = "locked_by = NULL, locked_until = NULL, lease_token = NULL"


def complete_step(
    conn: psycopg.Connection,
    job: Job,
    *,
    persist: Callable[[psycopg.Connection], None] | None = None,
    duplicate_of: str | None = None,
) -> str:
    """Persist the step's output and advance the state machine, atomically.

    Returns the recording's new status.
    """
    with owned(conn, job):
        if persist is not None:
            persist(conn)
        rec = _lock_recording(conn, job.tenant_id, job.recording_id)

        if duplicate_of is not None:
            to_status = "DUPLICATE"
            conn.execute(
                "UPDATE recordings SET status = 'DUPLICATE', duplicate_of = %s,"
                " current_step = NULL, error_code = NULL, error_message = NULL,"
                " updated_at = now() WHERE tenant_id = %s AND id = %s",
                (duplicate_of, job.tenant_id, job.recording_id),
            )
            conn.execute(
                f"UPDATE jobs SET state = 'done', {_CLEAR_LEASE}, last_error = NULL,"
                " updated_at = now() WHERE id = %s",
                (job.id,),
            )
            detail = f"byte-identical to {duplicate_of}; not sent to CRM"
        else:
            nxt = next_step(job.step)
            if nxt is None:
                to_status = "COMPLETED"
                conn.execute(
                    "UPDATE recordings SET status = 'COMPLETED', current_step = NULL,"
                    " error_code = NULL, error_message = NULL, updated_at = now()"
                    " WHERE tenant_id = %s AND id = %s",
                    (job.tenant_id, job.recording_id),
                )
                conn.execute(
                    f"UPDATE jobs SET state = 'done', {_CLEAR_LEASE}, last_error = NULL,"
                    " updated_at = now() WHERE id = %s",
                    (job.id,),
                )
                detail = None
            else:
                to_status = STATUS_FOR_STEP[nxt]
                conn.execute(
                    "UPDATE recordings SET status = %s, current_step = %s,"
                    " error_code = NULL, error_message = NULL, updated_at = now()"
                    " WHERE tenant_id = %s AND id = %s",
                    (to_status, nxt, job.tenant_id, job.recording_id),
                )
                conn.execute(
                    f"UPDATE jobs SET step = %s, state = 'queued', attempts = 0,"
                    f" run_at = now(), {_CLEAR_LEASE}, last_error = NULL, updated_at = now()"
                    " WHERE id = %s",
                    (nxt, job.id),
                )
                detail = f"next: {nxt}"
        _event(conn, job.tenant_id, job.recording_id,
               "duplicate_detected" if duplicate_of else "step_completed",
               step=job.step, from_status=rec["status"], to_status=to_status,
               detail=detail, worker=job.worker_id)
        return to_status


def fail_step(
    conn: psycopg.Connection,
    job: Job,
    *,
    code: str,
    message: str,
    permanent: bool,
    retry_after: float | None = None,
    failed_step: str | None = None,
) -> str:
    """Record a failure: reschedule (transient, attempts left) or mark FAILED.

    Returns the recording's new status.
    """
    message = (message or code)[:MAX_ERROR_LEN]
    with owned(conn, job):
        if not permanent and code.endswith("rate_limited"):
            # The provider asked us to wait (HTTP 429). That is not a failed attempt: wait
            # as told and give the attempt back. A separate cap keeps a quota that never
            # returns (daily limit) from waiting forever; past it, the attempt counts.
            waits = conn.execute(
                "SELECT count(*) AS n FROM recording_events WHERE tenant_id = %s"
                " AND recording_id = %s AND step = %s AND event = 'throttled'",
                (job.tenant_id, job.recording_id, job.step),
            ).fetchone()["n"]
            if waits < MAX_THROTTLE_WAITS:
                delay = (retry_after if retry_after is not None
                         else backoff_seconds(min(waits + 1, 6))) + random.uniform(0, 2)
                conn.execute(
                    f"UPDATE jobs SET state = 'queued', attempts = GREATEST(attempts - 1, 0),"
                    f" run_at = now() + make_interval(secs => %s), {_CLEAR_LEASE},"
                    f" last_error = %s, updated_at = now() WHERE id = %s",
                    (delay, f"{code}: {message}", job.id),
                )
                _event(conn, job.tenant_id, job.recording_id, "throttled", step=job.step,
                       detail=f"{code}: {message} (wait {waits + 1}/{MAX_THROTTLE_WAITS},"
                              f" retry in {delay:.1f}s; attempt not counted)",
                       worker=job.worker_id)
                return STATUS_FOR_STEP[job.step]
        if not permanent and job.attempts < job.max_attempts:
            delay = retry_after if retry_after is not None else backoff_seconds(job.attempts)
            conn.execute(
                f"UPDATE jobs SET state = 'queued', run_at = now() + make_interval(secs => %s),"
                f" {_CLEAR_LEASE}, last_error = %s, updated_at = now() WHERE id = %s",
                (delay, f"{code}: {message}", job.id),
            )
            conn.execute(
                "UPDATE recordings SET error_code = %s, error_message = %s, updated_at = now()"
                " WHERE tenant_id = %s AND id = %s",
                (code, message, job.tenant_id, job.recording_id),
            )
            _event(conn, job.tenant_id, job.recording_id, "retry_scheduled", step=job.step,
                   detail=f"{code}: {message} (attempt {job.attempts}/{job.max_attempts},"
                          f" retry in {delay:.1f}s)",
                   worker=job.worker_id)
            return STATUS_FOR_STEP[job.step]
        # failed_step can be more specific than the job's step (CRM -> CRM_BOOKING);
        # the job keeps its step, so reprocess resumes at PROCESSING_CRM.
        _mark_failed(conn, job.id, job.tenant_id, job.recording_id, failed_step or job.step, code,
                     message if permanent else
                     f"{message} (gave up after {job.attempts} attempts)",
                     job.worker_id)
        return "FAILED"


def _mark_failed(conn, job_id, tenant_id, recording_id, step, code, message, worker) -> None:
    rec = _lock_recording(conn, tenant_id, recording_id)
    conn.execute(
        "UPDATE recordings SET status = 'FAILED', failed_step = %s, current_step = NULL,"
        " error_code = %s, error_message = %s, updated_at = now()"
        " WHERE tenant_id = %s AND id = %s",
        (step, code, message[:MAX_ERROR_LEN], tenant_id, recording_id),
    )
    conn.execute(
        f"UPDATE jobs SET state = 'failed', {_CLEAR_LEASE}, last_error = %s,"
        " updated_at = now() WHERE id = %s",
        (f"{code}: {message}"[:MAX_ERROR_LEN], job_id),
    )
    _event(conn, tenant_id, recording_id, "failed", step=step, from_status=rec["status"],
           to_status="FAILED", detail=f"{code}: {message}", worker=worker)


# ------------------------------------------------------------------------ reprocess

def reprocess(
    conn: psycopg.Connection,
    tenant_id: str | None = None,
    recording_id: str | None = None,
) -> list[tuple[str, str, str]]:
    """Re-queue FAILED recordings at the step that failed. Earlier steps are not re-run:
    their outputs are already stored. Returns [(tenant, recording, step)]."""
    where, params = ["state = 'failed'"], []
    if tenant_id:
        where.append("tenant_id = %s")
        params.append(tenant_id)
    if recording_id:
        where.append("recording_id = %s")
        params.append(recording_id)
    candidates = conn.execute(
        f"SELECT id, tenant_id, recording_id FROM jobs WHERE {' AND '.join(where)}"
        " ORDER BY id",
        params,
    ).fetchall()

    requeued: list[tuple[str, str, str]] = []
    for cand in candidates:
        with conn.transaction():
            job = conn.execute(
                "SELECT id, tenant_id, recording_id, step FROM jobs"
                " WHERE id = %s AND state = 'failed' FOR UPDATE SKIP LOCKED",
                (cand["id"],),
            ).fetchone()
            if job is None:  # already re-queued by a concurrent reprocess
                continue
            set_tenant(conn, job["tenant_id"])
            rec = _lock_recording(conn, job["tenant_id"], job["recording_id"])
            if rec["status"] != "FAILED":
                continue
            to_status = STATUS_FOR_STEP[job["step"]]
            conn.execute(
                "UPDATE jobs SET state = 'queued', attempts = 0, run_at = now(),"
                " last_error = NULL, updated_at = now() WHERE id = %s",
                (job["id"],),
            )
            conn.execute(
                "UPDATE recordings SET status = %s, current_step = %s, failed_step = NULL,"
                " error_code = NULL, error_message = NULL, updated_at = now()"
                " WHERE tenant_id = %s AND id = %s",
                (to_status, job["step"], job["tenant_id"], job["recording_id"]),
            )
            _event(conn, job["tenant_id"], job["recording_id"], "reprocess_requested",
                   step=job["step"], from_status="FAILED", to_status=to_status,
                   detail=f"resuming at {job['step']}")
            requeued.append((job["tenant_id"], job["recording_id"], job["step"]))
    return requeued
