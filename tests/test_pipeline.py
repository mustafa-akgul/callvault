"""Queue + state machine: validation, duplicates, retries, reprocess, leases, parallelism."""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest
from psycopg import errors

from app import queue
from app.db import tenant_tx
from app.ingest import ingest_manifest
from app.steps import PermanentError, StepResult, TransientError, default_handlers
from app.worker import Worker
from tests.wavs import write_truncated_wav, write_wav


def rec(rid, at, tenant="harbor"):
    return {"id": rid, "tenant_id": tenant, "file": f"audio/{rid}.wav", "language": "tr",
            "channel": "phone", "recorded_at": at}


class Recorder:
    """Counts handler calls per (recording, step); thread-safe."""

    def __init__(self):
        self.calls = Counter()
        self.lock = threading.Lock()
        self.active: set = set()
        self.overlaps = 0

    def wrap(self, fn):
        def handler(ctx):
            key = (ctx.job.tenant_id, ctx.job.recording_id, ctx.job.step)
            with self.lock:
                self.calls[key] += 1
                if (key[0], key[1]) in self.active:
                    self.overlaps += 1
                self.active.add((key[0], key[1]))
            try:
                return fn(ctx)
            finally:
                with self.lock:
                    self.active.discard((key[0], key[1]))
        return handler


def handlers(recorder: Recorder, **overrides):
    h = default_handlers()
    ok = lambda ctx: StepResult()  # noqa: E731
    h.update(TRANSCRIPTION=ok, MASKING=ok, CRM=ok)
    h.update(overrides)
    return {step: recorder.wrap(fn) for step, fn in h.items()}


def make_worker(settings, manifest, hs, **kw):
    kw.setdefault("lease_seconds", 30)
    kw.setdefault("heartbeat_seconds", 5)
    return Worker(settings.worker_url, audio_root=manifest.parent, handlers=hs,
                  poll_seconds=0.05, **kw)


def drain(worker, limit=200):
    for _ in range(limit):
        if not worker.run_once():
            return
    raise AssertionError("queue did not drain")


def drain_with_waits(worker, admin_conn, timeout=10.0):
    """Like drain(), but also waits for jobs scheduled in the future (retries)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if worker.run_once():
            continue
        left = admin_conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')").fetchone()["n"]
        if left == 0:
            return
        time.sleep(0.05)
    raise AssertionError("queue did not drain in time")


def statuses(user_conn, tenant="harbor"):
    with tenant_tx(user_conn, tenant):
        return {r["id"]: r for r in user_conn.execute("SELECT * FROM recordings")}


@pytest.fixture
def dataset(tmp_path, make_manifest):
    """good, its byte-identical copy (later), a truncated file and a missing file."""
    audio = tmp_path / "audio"
    write_wav(audio / "rec_a.wav", seconds=2, freq=300)
    (audio / "rec_dup.wav").write_bytes((audio / "rec_a.wav").read_bytes())
    write_wav(audio / "rec_b.wav", seconds=2, freq=500)
    write_truncated_wav(audio / "rec_bad.wav", keep_bytes=2048, seconds=10)
    return make_manifest([
        rec("rec_dup", "2026-10-30T12:46:06+03:00"),   # listed first, recorded later
        rec("rec_a", "2026-10-30T08:49:28+03:00"),
        rec("rec_b", "2026-10-30T09:00:00+03:00"),
        rec("rec_bad", "2026-10-30T10:00:00+03:00"),
        rec("rec_missing", "2026-10-30T11:00:00+03:00"),
    ])


# ------------------------------------------------------------------ end to end

def test_full_run_reaches_a_terminal_state_for_every_recording(
        settings, dataset, worker_conn, user_conn):
    ingest_manifest(worker_conn, dataset)
    r = Recorder()
    w = make_worker(settings, dataset, handlers(r))
    drain(w)
    s = statuses(user_conn)

    assert s["rec_a"]["status"] == "COMPLETED"
    assert s["rec_b"]["status"] == "COMPLETED"

    assert s["rec_dup"]["status"] == "DUPLICATE"
    assert s["rec_dup"]["duplicate_of"] == "rec_a"
    assert ("harbor", "rec_dup", "TRANSCRIPTION") not in r.calls   # never transcribed
    assert ("harbor", "rec_dup", "CRM") not in r.calls             # never sent to CRM

    assert s["rec_bad"]["status"] == "FAILED"
    assert s["rec_bad"]["failed_step"] == "VALIDATION"
    assert s["rec_bad"]["error_code"] == "truncated_audio"

    assert s["rec_missing"]["status"] == "FAILED"
    assert s["rec_missing"]["error_code"] == "file_missing"

    # Nothing lost: every ingested recording is in a terminal state.
    assert {v["status"] for v in s.values()} <= {"COMPLETED", "DUPLICATE", "FAILED"}
    assert len(s) == 5


def test_ingest_is_idempotent(dataset, worker_conn, admin_conn):
    first = ingest_manifest(worker_conn, dataset)
    second = ingest_manifest(worker_conn, dataset)
    assert (first.created, second.created, second.already_present) == (5, 0, 5)
    assert admin_conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 5


def test_status_moves_through_the_state_machine(settings, dataset, worker_conn, user_conn):
    ingest_manifest(worker_conn, dataset)
    seen = []

    def spy(ctx):
        if ctx.job.recording_id == "rec_a":
            with tenant_tx(ctx.conn, ctx.job.tenant_id):
                seen.append(ctx.conn.execute("SELECT status FROM recordings WHERE id = %s",
                                             (ctx.job.recording_id,)).fetchone()["status"])
        return StepResult()

    h = default_handlers()
    h.update(TRANSCRIPTION=spy, MASKING=spy, CRM=spy)
    w = make_worker(settings, dataset, h)
    drain(w)
    assert seen == ["TRANSCRIBING", "MASKING", "PROCESSING_CRM"]
    with tenant_tx(user_conn, "harbor"):
        events = [e["to_status"] for e in user_conn.execute(
            "SELECT to_status FROM recording_events WHERE recording_id = 'rec_a'"
            " AND to_status IS NOT NULL ORDER BY id")]
    assert events == ["PENDING", "VALIDATING", "TRANSCRIBING", "TRANSCRIBING", "MASKING",
                      "MASKING", "PROCESSING_CRM", "PROCESSING_CRM", "COMPLETED"]


def test_duplicate_choice_does_not_depend_on_processing_order(
        settings, dataset, worker_conn, admin_conn, user_conn):
    ingest_manifest(worker_conn, dataset)
    # Make the later copy get validated first.
    admin_conn.execute("UPDATE jobs SET run_at = now() + interval '1 hour'"
                       " WHERE recording_id = 'rec_a'")
    w = make_worker(settings, dataset, handlers(Recorder()))
    drain(w)
    admin_conn.execute("UPDATE jobs SET run_at = now() WHERE recording_id = 'rec_a'")
    drain(w)
    s = statuses(user_conn)
    assert s["rec_dup"]["status"] == "DUPLICATE" and s["rec_dup"]["duplicate_of"] == "rec_a"
    assert s["rec_a"]["status"] == "COMPLETED"


def test_duplicates_are_per_tenant(settings, tmp_path, make_manifest, worker_conn, user_conn):
    audio = tmp_path / "audio"
    write_wav(audio / "same.wav", seconds=2)
    m = make_manifest([
        {**rec("same", "2026-10-30T08:00:00+03:00"), "file": "audio/same.wav"},
        {**rec("same", "2026-10-30T09:00:00+03:00", tenant="acme"), "file": "audio/same.wav"},
    ])
    ingest_manifest(worker_conn, m)
    drain(make_worker(settings, m, handlers(Recorder())))
    assert statuses(user_conn, "harbor")["same"]["status"] == "COMPLETED"
    assert statuses(user_conn, "acme")["same"]["status"] == "COMPLETED"


def test_duplicate_can_never_get_a_crm_action(settings, dataset, worker_conn):
    ingest_manifest(worker_conn, dataset)
    drain(make_worker(settings, dataset, handlers(Recorder())))
    with pytest.raises(errors.CheckViolation, match="duplicate"):
        with tenant_tx(worker_conn, "harbor"):
            worker_conn.execute("INSERT INTO crm_actions (tenant_id, recording_id, action_type)"
                                " VALUES ('harbor', 'rec_dup', 'booking')")


# ------------------------------------------------------------ retries / reprocess

def test_transient_errors_are_retried(settings, dataset, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, dataset)
    failures = Counter()

    def flaky(ctx):
        if failures[ctx.job.recording_id] < 2:
            failures[ctx.job.recording_id] += 1
            raise TransientError("stt_unavailable", "HTTP 503", retry_after=0)
        return StepResult()

    r = Recorder()
    drain_with_waits(make_worker(settings, dataset, handlers(r, TRANSCRIPTION=flaky)),
                     admin_conn)
    s = statuses(user_conn)
    assert s["rec_a"]["status"] == "COMPLETED"
    assert s["rec_a"]["retry_count"] == 2
    assert s["rec_a"]["error_code"] is None
    assert r.calls[("harbor", "rec_a", "TRANSCRIPTION")] == 3


def test_exhausted_retries_fail_and_reprocess_resumes_at_failed_step(
        settings, dataset, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, dataset, max_attempts=3)
    r = Recorder()

    def always_down(ctx):
        raise TransientError("stt_unavailable", "HTTP 503", retry_after=0)

    drain_with_waits(make_worker(settings, dataset, handlers(r, TRANSCRIPTION=always_down)),
                     admin_conn)
    s = statuses(user_conn)
    assert s["rec_a"]["status"] == "FAILED"
    assert s["rec_a"]["failed_step"] == "TRANSCRIPTION"
    assert s["rec_a"]["error_code"] == "stt_unavailable"
    assert "gave up after 3 attempts" in s["rec_a"]["error_message"]
    assert r.calls[("harbor", "rec_a", "TRANSCRIPTION")] == 3

    requeued = queue.reprocess(worker_conn, tenant_id="harbor", recording_id="rec_a")
    assert requeued == [("harbor", "rec_a", "TRANSCRIPTION")]
    assert statuses(user_conn)["rec_a"]["status"] == "TRANSCRIBING"

    drain(make_worker(settings, dataset, handlers(r)))
    s = statuses(user_conn)
    assert s["rec_a"]["status"] == "COMPLETED"
    # VALIDATION succeeded before and was NOT repeated.
    assert r.calls[("harbor", "rec_a", "VALIDATION")] == 1
    assert r.calls[("harbor", "rec_a", "TRANSCRIPTION")] == 4


def test_permanent_error_fails_immediately(settings, dataset, worker_conn, user_conn):
    ingest_manifest(worker_conn, dataset)
    r = Recorder()

    def bad_key(ctx):
        raise PermanentError("stt_auth_error", "HTTP 401 invalid API key")

    drain(make_worker(settings, dataset, handlers(r, TRANSCRIPTION=bad_key)))
    s = statuses(user_conn)
    assert (s["rec_a"]["status"], s["rec_a"]["failed_step"], s["rec_a"]["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "stt_auth_error")
    assert r.calls[("harbor", "rec_a", "TRANSCRIPTION")] == 1

    # Reprocess everything that failed: the validation failures fail again (the file is
    # still broken), the transcription failures resume at TRANSCRIPTION.
    requeued = {(t, rid, step) for t, rid, step in queue.reprocess(worker_conn)}
    assert ("harbor", "rec_a", "TRANSCRIPTION") in requeued
    assert ("harbor", "rec_bad", "VALIDATION") in requeued


def test_unexpected_exception_does_not_drop_the_recording(
        settings, dataset, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, dataset, max_attempts=2)

    def buggy(ctx):
        raise KeyError("oops")

    w = make_worker(settings, dataset, handlers(Recorder(), MASKING=buggy))
    # Backoff makes the retry wait ~2-3s; skip the wait.
    for _ in range(4):
        drain(w)
        admin_conn.execute("UPDATE jobs SET run_at = now() WHERE state = 'queued'")
    s = statuses(user_conn)
    assert (s["rec_a"]["status"], s["rec_a"]["failed_step"], s["rec_a"]["error_code"]) == \
        ("FAILED", "MASKING", "unexpected_error")


# --------------------------------------------------------- leases and fencing

def test_expired_lease_is_reclaimed_and_zombie_is_fenced_off(
        settings, dataset, worker_conn, user_conn):
    ingest_manifest(worker_conn, dataset)
    zombie = queue.claim(worker_conn, "zombie", lease_seconds=0.2)
    assert zombie is not None and not zombie.reclaimed
    time.sleep(0.4)  # zombie froze / was SIGKILLed; its lease runs out

    rescuer = queue.claim(worker_conn, "rescuer", lease_seconds=30)
    assert rescuer.id == zombie.id and rescuer.reclaimed
    assert rescuer.attempts == 2

    # The zombie wakes up: it can neither finish nor fail the job any more.
    with pytest.raises(queue.LeaseLost):
        queue.complete_step(worker_conn, zombie)
    with pytest.raises(queue.LeaseLost):
        queue.fail_step(worker_conn, zombie, code="x", message="x", permanent=True)
    assert not queue.heartbeat(worker_conn, zombie, 30)

    assert queue.complete_step(worker_conn, rescuer) == "TRANSCRIBING"
    with tenant_tx(user_conn, "harbor"):
        ev = [e["event"] for e in user_conn.execute(
            "SELECT event FROM recording_events WHERE recording_id = %s ORDER BY id",
            (rescuer.recording_id,))]
    assert ev == ["ingested", "claimed", "lease_reclaimed", "step_completed"]


def test_heartbeat_keeps_a_live_lease(dataset, worker_conn):
    ingest_manifest(worker_conn, dataset)
    job = queue.claim(worker_conn, "alive", lease_seconds=0.5)
    for _ in range(4):
        time.sleep(0.2)
        assert queue.heartbeat(worker_conn, job, 0.5)
    assert queue.claim(worker_conn, "other", lease_seconds=30).id != job.id


def test_job_that_keeps_killing_workers_ends_failed(dataset, worker_conn, user_conn):
    ingest_manifest(worker_conn, dataset, max_attempts=2)
    first = queue.claim(worker_conn, "w1", lease_seconds=0.1)
    rid = first.recording_id
    time.sleep(0.2)
    second = queue.claim(worker_conn, "w2", lease_seconds=0.1)
    assert second.id == first.id
    time.sleep(0.2)
    third = queue.claim(worker_conn, "w3", lease_seconds=30)  # marks it failed, moves on
    assert third is None or third.id != first.id
    s = statuses(user_conn)[rid]
    assert (s["status"], s["failed_step"], s["error_code"]) == \
        ("FAILED", "VALIDATION", "worker_lost")


def test_parallel_workers_never_process_the_same_recording_twice(
        settings, tmp_path, make_manifest, worker_conn, user_conn):
    audio = tmp_path / "audio"
    recs = []
    for i in range(24):
        write_wav(audio / f"r{i:02d}.wav", seconds=1.2, freq=200 + i * 10)
        recs.append(rec(f"r{i:02d}", f"2026-10-30T{8 + i // 6:02d}:{i % 6 * 10:02d}:00+03:00"))
    m = make_manifest(recs)
    ingest_manifest(worker_conn, m)

    r = Recorder()
    slow = lambda ctx: (time.sleep(0.02), StepResult())[1]  # noqa: E731
    hs = handlers(r, TRANSCRIPTION=slow, MASKING=slow, CRM=slow)
    stop = threading.Event()
    workers = [make_worker(settings, m, hs, worker_id=f"w{i}") for i in range(4)]
    threads = [threading.Thread(target=w.run, args=(stop, True)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for w in workers:
        w.close()

    s = statuses(user_conn)
    assert len(s) == 24 and all(v["status"] == "COMPLETED" for v in s.values())
    assert r.overlaps == 0
    assert all(n == 1 for n in r.calls.values()), {k: n for k, n in r.calls.items() if n > 1}
    assert len(r.calls) == 24 * 4
