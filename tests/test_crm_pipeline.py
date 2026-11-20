"""PROCESSING_CRM end to end: real queue, worker and DB; in-memory CRM with faults."""

from __future__ import annotations

import random
import threading
from datetime import datetime

import pytest

from app import queue
from app.crm import CRMClient
from app.crypto import TranscriptCipher
from app.db import tenant_tx
from app.ingest import ingest_manifest
from app.steps import build_handlers
from app.stt import ChannelTranscript, Segment
from app.worker import Worker
from tests.fake_crm import FakeCRM
from tests.wavs import write_wav

AGENT = [("Harbor müşteri hizmetleri, size nasıl yardımcı olabilirim?", 0.0, 2.0)]

SCRIPTS = {
    "meet_and_complain": [
        ("Merhaba, adım Ayşe Yılmaz. Faturam iki kez çekildi, iade istiyorum.", 2.5, 6.0),
        ("Bir de ekibinizle toplantı yapmak istiyorum, pazartesi saat 15'te olur mu?", 6.5, 9.0),
    ],
    "meet_only": [("Sizinle bir toplantı ayarlamak istiyorum, pazartesi saat 15'te.", 2.5, 5.0)],
    "complaint_only": [("Kargom iki haftadır gecikti, çok memnun değilim.", 2.5, 5.0)],
    "nothing": [("Sadece çalışma saatlerinizi sormak istedim, teşekkürler.", 2.5, 5.0)],
    "meet_full_day": [("3 Kasım öğleden sonra bir görüşme rica ediyorum.", 2.5, 5.0)],
}


class ScriptedSTT:
    name, model = "fake", "fake-1"

    def __init__(self):
        self.calls = 0

    def transcribe(self, wav, filename, language):
        self.calls += 1
        rid = filename.rsplit("_ch", 1)[0]
        lines = AGENT if filename.endswith("_ch0.wav") else SCRIPTS[rid.split("__")[0]]
        segs = [Segment(a, b, t) for t, a, b in lines]
        return ChannelTranscript(segs, " ".join(s.text for s in segs), "tr", 10.0)


@pytest.fixture
def crm():
    return FakeCRM()


def crm_client(crm, max_attempts=4):
    return CRMClient("http://crm", transport=crm.transport(), sleep=lambda s: None,
                     min_interval=0, max_attempts=max_attempts, rng=random.Random(0))


def make_calls(tmp_path, make_manifest, *ids, at="2026-10-30T09:00:00+03:00"):
    recs = []
    for i, rid in enumerate(ids):
        write_wav(tmp_path / "audio" / f"{rid}.wav", seconds=1.5, freq=200 + 40 * i)
        recs.append({"id": rid, "tenant_id": "harbor", "file": f"audio/{rid}.wav",
                     "language": "tr", "recorded_at": at})
    return make_manifest(recs)


def run(settings, manifest, stt, client, admin_conn=None, rounds=1, crm_handler=None):
    hs = build_handlers(stt=stt, cipher=TranscriptCipher.from_env(), crm=client)
    if crm_handler:
        hs["CRM"] = crm_handler(hs["CRM"])
    w = Worker(settings.worker_url, audio_root=manifest.parent, handlers=hs,
               lease_seconds=30, heartbeat_seconds=5, poll_seconds=0.01)
    try:
        for _ in range(rounds):
            while w.run_once():
                pass
            if admin_conn is not None:
                admin_conn.execute("UPDATE jobs SET run_at = now() WHERE state = 'queued'")
    finally:
        w.close()


def rec_row(user_conn, rid):
    with tenant_tx(user_conn, "harbor"):
        return user_conn.execute("SELECT * FROM recordings WHERE id = %s", (rid,)).fetchone()


def actions(user_conn, rid):
    with tenant_tx(user_conn, "harbor"):
        return {a["action_type"]: a for a in user_conn.execute(
            "SELECT * FROM crm_actions WHERE recording_id = %s", (rid,))}


def attempts(user_conn, rid):
    with tenant_tx(user_conn, "harbor"):
        return user_conn.execute(
            "SELECT * FROM crm_booking_attempts WHERE recording_id = %s ORDER BY id",
            (rid,)).fetchall()


# ------------------------------------------------------------------ happy path

def test_meeting_and_complaint_are_delivered(settings, tmp_path, make_manifest, worker_conn,
                                              user_conn, crm):
    m = make_calls(tmp_path, make_manifest, "meet_and_complain")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))

    assert rec_row(user_conn, "meet_and_complain")["status"] == "COMPLETED"
    a = actions(user_conn, "meet_and_complain")
    b, c = a["booking"], a["complaint"]
    assert b["status"] == "SUCCEEDED"
    assert b["booked_slot_start"] == datetime.fromisoformat("2026-11-02T15:00:00+03:00")
    assert b["requested_start"] == b["booked_slot_start"]
    assert "pazartesi saat 15'te" in b["requested_text"]
    assert b["requested_window"]["exact"] == "2026-11-02T15:00:00+03:00"
    assert c["status"] == "SUCCEEDED"
    assert c["idempotency_key"] == "harbor:complaint:meet_and_complain"
    assert c["request_payload"]["summary"].startswith("Müşteri şikayeti: ")
    assert "Ayşe" not in c["request_payload"]["summary"]         # masked text only

    assert [x["call_id"] for x in crm.bookings] == ["meet_and_complain"]
    assert crm.bookings[0]["slot_start"] == "2026-11-02T15:00:00+03:00"
    assert [x["call_id"] for x in crm.complaints] == ["meet_and_complain"]
    att = attempts(user_conn, "meet_and_complain")
    assert [x["idempotency_key"] for x in att] == \
        ["harbor:booking:meet_and_complain:2026-11-02T15:00:00+03:00"]


def test_call_without_actions_completes_without_crm_writes(
        settings, tmp_path, make_manifest, worker_conn, user_conn, crm):
    m = make_calls(tmp_path, make_manifest, "nothing")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    assert rec_row(user_conn, "nothing")["status"] == "COMPLETED"
    assert actions(user_conn, "nothing") == {}
    assert crm.posts("/bookings") == crm.posts("/complaints") == []


def test_no_matching_slot_needs_followup_and_sends_nothing(
        settings, tmp_path, make_manifest, worker_conn, user_conn, crm):
    for h in (13, 14, 15):
        crm.take(f"2026-11-03T{h}:00:00+03:00")          # 16, 17 are pre-booked
    m = make_calls(tmp_path, make_manifest, "meet_full_day")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    assert rec_row(user_conn, "meet_full_day")["status"] == "COMPLETED"
    b = actions(user_conn, "meet_full_day")["booking"]
    assert b["status"] == "NEEDS_FOLLOWUP" and "no free slot" in b["followup_reason"]
    assert b["requested_window"]["date_from"] == "2026-11-03"
    assert crm.posts("/bookings") == []


# ------------------------------------------------------- chaos / no duplicates

def test_lost_response_does_not_move_or_duplicate_the_booking(
        settings, tmp_path, make_manifest, worker_conn, user_conn, crm):
    crm.fail("POST", "/bookings", "drop_after_commit")
    m = make_calls(tmp_path, make_manifest, "meet_only")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    assert len(crm.bookings) == 1
    assert crm.bookings[0]["slot_start"] == "2026-11-02T15:00:00+03:00"   # not moved
    assert actions(user_conn, "meet_only")["booking"]["status"] == "SUCCEEDED"


def test_crash_after_crm_commit_is_adopted_on_retry(
        settings, tmp_path, make_manifest, worker_conn, user_conn, admin_conn, crm):
    """The CRM wrote the booking; the worker died before recording it."""
    m = make_calls(tmp_path, make_manifest, "meet_only")
    ingest_manifest(worker_conn, m)
    client = crm_client(crm)
    real = client.create_booking
    state = {"crash": True}

    def dying(body, key):
        resp = real(body, key)
        if state["crash"]:
            state["crash"] = False
            raise RuntimeError("worker died right after the CRM answered")
        return resp

    client.create_booking = dying
    run(settings, m, ScriptedSTT(), client, admin_conn=admin_conn, rounds=3)

    assert rec_row(user_conn, "meet_only")["status"] == "COMPLETED"
    assert len(crm.bookings) == 1 and len(crm.posts("/bookings")) == 1   # adopted, not resent
    att = attempts(user_conn, "meet_only")
    assert [a["status"] for a in att] == ["ADOPTED"]


def test_conflict_is_the_only_way_to_a_new_slot_and_key(
        settings, tmp_path, make_manifest, worker_conn, user_conn, crm):
    stolen = {"done": False}

    def steal(body):        # someone else takes the slot between GET /slots and POST
        if not stolen["done"]:
            stolen["done"] = True
            crm.take(body["slot_start"])

    crm.before_post_booking = steal
    m = make_calls(tmp_path, make_manifest, "meet_only")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))

    att = attempts(user_conn, "meet_only")
    assert [(a["status"], a["slot_start_text"]) for a in att] == [
        ("CONFLICT", "2026-11-02T15:00:00+03:00"),
        ("SUCCEEDED", "2026-11-02T16:00:00+03:00"),
    ]
    assert [a["idempotency_key"] for a in att] == [
        "harbor:booking:meet_only:2026-11-02T15:00:00+03:00",
        "harbor:booking:meet_only:2026-11-02T16:00:00+03:00",
    ]
    assert [b["slot_start"] for b in crm.bookings] == ["2026-11-02T16:00:00+03:00"]
    assert actions(user_conn, "meet_only")["booking"]["booked_slot_start"] == \
        datetime.fromisoformat("2026-11-02T16:00:00+03:00")


def test_crm_outage_fails_at_crm_booking_and_reprocess_resumes_there(
        settings, tmp_path, make_manifest, worker_conn, user_conn, admin_conn, crm):
    crm.fail("POST", "/bookings", *["503"] * 40)
    m = make_calls(tmp_path, make_manifest, "meet_only")
    ingest_manifest(worker_conn, m, max_attempts=2)
    stt = ScriptedSTT()
    run(settings, m, stt, crm_client(crm, max_attempts=3), admin_conn=admin_conn, rounds=3)

    r = rec_row(user_conn, "meet_only")
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "CRM_BOOKING", "crm_unavailable")
    assert "[booking]" in r["error_message"] and "HTTP 503" in r["error_message"]
    assert crm.bookings == []
    calls_before = stt.calls

    crm.faults.clear()                                   # CRM is back
    assert queue.reprocess(worker_conn) == [("harbor", "meet_only", "CRM")]
    assert rec_row(user_conn, "meet_only")["status"] == "PROCESSING_CRM"
    run(settings, m, stt, crm_client(crm))

    assert rec_row(user_conn, "meet_only")["status"] == "COMPLETED"
    assert stt.calls == calls_before                      # STT / masking not repeated
    att = attempts(user_conn, "meet_only")
    assert len(att) == 1 and att[0]["status"] == "SUCCEEDED" and att[0]["sends"] >= 3
    keys = {r[2] for r in crm.posts("/bookings")}
    assert keys == {att[0]["idempotency_key"]}            # one key across every retry
    assert len(crm.bookings) == 1


def test_rejected_complaint_fails_at_crm_complaint(
        settings, tmp_path, make_manifest, worker_conn, user_conn, crm):
    real = crm._complain
    crm._complain = lambda body: (422, {"error": "summary_too_long"})
    m = make_calls(tmp_path, make_manifest, "complaint_only")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    r = rec_row(user_conn, "complaint_only")
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "CRM_COMPLAINT", "crm_rejected")
    assert "422" in r["error_message"]
    crm._complain = real


def test_rerun_against_the_same_crm_creates_nothing_new(
        settings, tmp_path, make_manifest, worker_conn, user_conn, admin_conn, crm):
    """Our database is wiped and the whole dataset is processed again: the CRM already
    has the booking and complaint, so they are adopted, not created twice."""
    m = make_calls(tmp_path, make_manifest, "meet_and_complain")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    assert (len(crm.bookings), len(crm.complaints)) == (1, 1)

    admin_conn.execute("TRUNCATE recordings, tenants RESTART IDENTITY CASCADE")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    assert rec_row(user_conn, "meet_and_complain")["status"] == "COMPLETED"
    assert (len(crm.bookings), len(crm.complaints)) == (1, 1)
    assert len(crm.posts("/bookings")) == 1 and len(crm.posts("/complaints")) == 1


def test_parallel_workers_same_requested_slot(settings, tmp_path, make_manifest, worker_conn,
                                              user_conn, crm):
    ids = [f"meet_only__{i}" for i in range(4)]
    m = make_calls(tmp_path, make_manifest, *ids)
    ingest_manifest(worker_conn, m)
    stt = ScriptedSTT()
    client = crm_client(crm)
    hs = build_handlers(stt=stt, cipher=TranscriptCipher.from_env(), crm=client)
    stop = threading.Event()
    workers = [Worker(settings.worker_url, audio_root=m.parent, handlers=hs,
                      poll_seconds=0.01, worker_id=f"w{i}") for i in range(3)]
    threads = [threading.Thread(target=w.run, args=(stop, True)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for w in workers:
        w.close()

    assert all(rec_row(user_conn, rid)["status"] == "COMPLETED" for rid in ids)
    call_ids = [b["call_id"] for b in crm.bookings]
    assert sorted(call_ids) == sorted(ids)                  # one booking each
    slots = sorted(b["slot_start"] for b in crm.bookings)
    assert slots == ["2026-11-02T15:00:00+03:00", "2026-11-02T16:00:00+03:00",
                     "2026-11-02T17:00:00+03:00", "2026-11-03T10:00:00+03:00"]
