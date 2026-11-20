"""Kill a real worker process mid-job; a second worker must pick the job up."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from app.db import tenant_tx
from app.ingest import ingest_manifest
from tests.conftest import REPO
from tests.wavs import write_wav


def _worker(manifest, **env):
    full_env = {**os.environ, "PYTHONPATH": str(REPO), "LEASE_SECONDS": "2",
                "HEARTBEAT_SECONDS": "0.5", "POLL_SECONDS": "0.1", **env}
    return subprocess.Popen(
        [sys.executable, "-m", "app.cli", "worker", "--manifest", str(manifest),
         "--handlers", "tests.fake_handlers:slow_handlers"],
        cwd=REPO, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def test_sigkilled_worker_job_is_recovered(tmp_path, make_manifest, worker_conn, user_conn,
                                           admin_conn):
    write_wav(tmp_path / "audio" / "victim.wav", seconds=2)
    m = make_manifest([{"id": "victim", "tenant_id": "harbor", "file": "audio/victim.wav",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    ingest_manifest(worker_conn, m)

    first = _worker(m, FAKE_STEP_DELAY="60")
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            job = admin_conn.execute("SELECT step, state, locked_by FROM jobs").fetchone()
            if job["step"] == "TRANSCRIPTION" and job["state"] == "running":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("worker never started TRANSCRIPTION:\n" + first.stdout.read())
        killed_holder = job["locked_by"]
        first.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs
        first.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.kill()

    with tenant_tx(user_conn, "harbor"):
        mid = user_conn.execute("SELECT status FROM recordings").fetchone()
    assert mid["status"] == "TRANSCRIBING"  # visible, not lost

    # A second, long-running worker picks the job up as soon as the dead worker's lease
    # (LEASE_SECONDS=2) runs out; nobody renews it.
    second = _worker(m, FAKE_STEP_DELAY="0")
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            with tenant_tx(user_conn, "harbor"):
                status = user_conn.execute("SELECT status FROM recordings").fetchone()["status"]
            if status == "COMPLETED":
                break
            assert second.poll() is None, second.stdout.read()
            time.sleep(0.2)
    finally:
        second.terminate()           # SIGTERM: graceful stop
        out, _ = second.communicate(timeout=30)

    with tenant_tx(user_conn, "harbor"):
        final = user_conn.execute("SELECT status, retry_count FROM recordings").fetchone()
        events = user_conn.execute(
            "SELECT event, worker, detail FROM recording_events ORDER BY id").fetchall()
    assert final["status"] == "COMPLETED"
    assert final["retry_count"] == 1
    reclaim = [e for e in events if e["event"] == "lease_reclaimed"]
    assert len(reclaim) == 1 and killed_holder in reclaim[0]["detail"]
