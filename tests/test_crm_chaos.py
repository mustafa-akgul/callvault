"""The real crm-sim/server.py with CHAOS=1: rate limits, 500s, 8 s stalls and lost
responses, against 3 parallel workers. Nothing may be duplicated or lost.

Slow by nature (the CRM allows 2 requests/second and stalls 8 s on 10% of requests).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from app.crm import CRMClient
from app.crypto import TranscriptCipher
from app.db import tenant_tx
from app.ingest import ingest_manifest
from app.steps import build_handlers
from app.worker import Worker
from tests.conftest import REPO
from tests.test_crm_pipeline import ScriptedSTT, make_calls


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def chaos_crm(tmp_path):
    port = _free_port()
    seed = os.environ.get("CHAOS_TEST_SEED", "20261108")
    env = {**os.environ, "PORT": str(port), "CHAOS": "1", "CHAOS_SEED": seed,
           "STATE_FILE": str(tmp_path / "crm_state.json")}
    log = open(tmp_path / "crm.log", "w")
    proc = subprocess.Popen([sys.executable, str(REPO / "crm-sim" / "server.py")], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=1).json()["chaos"] is True:
                break
        except httpx.HTTPError:
            pass
        assert time.time() < deadline, "crm-sim did not start"
        time.sleep(0.1)
    yield url
    proc.kill()
    proc.wait(timeout=10)
    log.close()
    text = (tmp_path / "crm.log").read_text()
    counts = {code: text.count(f'" {code} ') for code in (201, 409, 429, 500)}
    print(f"\n[crm-sim seed={seed}] responses by status: {counts}, "
          f"requests: {text.count('HTTP/1.1')}")


def test_no_duplicates_and_nothing_lost_under_chaos(settings, tmp_path, make_manifest,
                                                     worker_conn, user_conn, chaos_crm):
    ids = ([f"meet_and_complain__{i}" for i in range(3)]
           + [f"meet_only__{i}" for i in range(2)]
           + [f"complaint_only__{i}" for i in range(2)]
           + ["nothing__0"])
    m = make_calls(tmp_path, make_manifest, *ids)
    ingest_manifest(worker_conn, m, max_attempts=10)

    def handlers():
        client = CRMClient(chaos_crm, timeout=12, max_attempts=8, backoff_base=0.2,
                           backoff_cap=2.0)
        return build_handlers(stt=ScriptedSTT(), cipher=TranscriptCipher.from_env(),
                              crm=client)

    stop = threading.Event()
    workers = [Worker(settings.worker_url, audio_root=m.parent, handlers=handlers(),
                      lease_seconds=60, heartbeat_seconds=5, poll_seconds=0.1,
                      worker_id=f"chaos-w{i}") for i in range(3)]
    threads = [threading.Thread(target=w.run, args=(stop,)) for w in workers]
    for t in threads:
        t.start()
    try:
        deadline = time.time() + 240
        while time.time() < deadline:
            with tenant_tx(user_conn, "harbor"):
                states = {r["id"]: r["status"] for r in user_conn.execute(
                    "SELECT id, status FROM recordings")}
            if all(s in ("COMPLETED", "FAILED") for s in states.values()):
                break
            time.sleep(0.5)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=60)
        for w in workers:
            w.close()

    assert states == {rid: "COMPLETED" for rid in ids}, states

    bookings = httpx.get(f"{chaos_crm}/bookings").json()["bookings"]
    complaints = httpx.get(f"{chaos_crm}/complaints").json()["complaints"]
    booked = sorted(b["call_id"] for b in bookings)
    complained = sorted(c["call_id"] for c in complaints)
    assert booked == sorted(i for i in ids if i.startswith("meet")), booked
    assert complained == sorted(i for i in ids if "complain" in i), complained
    assert len({b["slot_start"] for b in bookings}) == len(bookings)

    # Our records agree with the CRM, booking by booking.
    with tenant_tx(user_conn, "harbor"):
        ours = {a["recording_id"]: a for a in user_conn.execute(
            "SELECT * FROM crm_actions WHERE action_type = 'booking'")}
    for b in bookings:
        assert ours[b["call_id"]]["external_id"] == b["id"]
        assert ours[b["call_id"]]["status"] == "SUCCEEDED"
