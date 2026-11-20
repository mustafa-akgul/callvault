"""TRANSCRIPTION + MASKING through the real queue, worker and database."""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest
from psycopg import errors

from app import queue
from app.crypto import TranscriptCipher
from app.db import tenant_tx
from app.ingest import ingest_manifest
from app.steps import StepResult, build_handlers
from app.stt import ChannelTranscript, GroqSTT, Segment
from app.worker import Worker
from tests.wavs import write_wav

AGENT_LINES = [("Harbor müşteri hizmetleri, ben Zeynep, size nasıl yardımcı olabilirim?", 0.0, 3.0),
               ("Ayşe Hanım, kaydınızı oluşturuyorum.", 9.0, 11.0)]
CUSTOMER_LINES = [("Merhaba, adım Ayşe Yılmaz.", 3.5, 5.0),
                  ("Numaram sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi,", 5.0, 7.5),
                  ("mailim ayse.yilmaz@example.com", 7.5, 8.8)]
PII = ["Ayşe", "Yılmaz", "Zeynep", "0532", "532 123", "ayse.yilmaz@example.com", "altmış yedi"]


class FakeSTT:
    """Channel 0 = agent, channel 1 = customer. Counts calls."""

    name, model = "fake", "fake-1"

    def __init__(self):
        self.calls = 0

    def transcribe(self, wav, filename, language):
        self.calls += 1
        lines = AGENT_LINES if filename.endswith("_ch0.wav") else CUSTOMER_LINES
        segs = [Segment(a, b, t) for t, a, b in lines]
        return ChannelTranscript(segs, " ".join(s.text for s in segs), "tr", 12.0)


def ok(ctx):
    return StepResult()


def handlers(stt, **overrides):
    h = build_handlers(stt=stt, cipher=TranscriptCipher.from_env())
    h["CRM"] = ok
    h.update(overrides)
    return h


def run(settings, manifest, hs, admin_conn=None, rounds=1):
    w = Worker(settings.worker_url, audio_root=manifest.parent, handlers=hs,
               lease_seconds=30, heartbeat_seconds=5, poll_seconds=0.01)
    try:
        for _ in range(rounds):
            while w.run_once():
                pass
            if admin_conn is not None:  # skip retry backoff waits
                admin_conn.execute("UPDATE jobs SET run_at = now() WHERE state = 'queued'")
    finally:
        w.close()


def recording(user_conn, rid="call", tenant="harbor"):
    with tenant_tx(user_conn, tenant):
        return user_conn.execute("SELECT * FROM recordings WHERE id = %s", (rid,)).fetchone()


@pytest.fixture
def call(tmp_path, make_manifest):
    write_wav(tmp_path / "audio" / "call.wav", seconds=2)
    return make_manifest([{"id": "call", "tenant_id": "harbor", "file": "audio/call.wav",
                           "language": "tr", "recorded_at": "2026-10-30T09:00:00+03:00"}])


def test_transcribe_and_mask_end_to_end(settings, call, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, call)
    stt = FakeSTT()
    run(settings, call, handlers(stt))

    assert recording(user_conn)["status"] == "COMPLETED"
    assert stt.calls == 2  # one request per channel

    with tenant_tx(user_conn, "harbor"):
        t = user_conn.execute(
            "SELECT masked_text, segments, pii_summary, stt_meta, provider, model"
            " FROM transcripts WHERE recording_id = 'call'").fetchone()
    lines = t["masked_text"].splitlines()
    assert lines[0].startswith("Temsilci: Harbor müşteri hizmetleri, ben [İSİM MASKELENDİ],")
    assert lines[1] == ("Müşteri: Merhaba, adım [İSİM MASKELENDİ]. Numaram "
                        "[TELEFON MASKELENDİ], mailim [E-POSTA MASKELENDİ]")
    assert lines[2] == "Temsilci: [İSİM MASKELENDİ] Hanım, kaydınızı oluşturuyorum."
    for secret in PII:
        assert secret not in t["masked_text"]
        assert secret not in json.dumps(t["segments"], ensure_ascii=False)
    assert t["pii_summary"] == {"NAME": 3, "PHONE": 1, "EMAIL": 1}
    assert t["stt_meta"]["roles"] == {"0": "Temsilci", "1": "Müşteri"}
    assert (t["provider"], t["model"]) == ("fake", "fake-1")

    # Raw text is stored, but only encrypted, and bound to its row.
    raw = admin_conn.execute("SELECT raw_text_enc FROM transcripts").fetchone()["raw_text_enc"]
    for secret in ("Ayşe", "0532", "ayse.yilmaz"):
        assert secret.encode() not in bytes(raw)
    doc = json.loads(TranscriptCipher.from_env().decrypt(raw, "harbor/call"))
    assert "ayse.yilmaz@example.com" in doc["text"]
    assert doc["segments"][0]["speaker"] == "Temsilci"


def test_read_role_cannot_select_raw_transcript(settings, call, worker_conn, user_conn):
    ingest_manifest(worker_conn, call)
    run(settings, call, handlers(FakeSTT()))
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        with tenant_tx(user_conn, "harbor"):
            user_conn.execute("SELECT raw_text_enc FROM transcripts").fetchall()


def test_ciphertext_moved_to_another_row_does_not_decrypt(
        settings, call, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, call)
    run(settings, call, handlers(FakeSTT()))
    blob = admin_conn.execute("SELECT raw_text_enc FROM transcripts").fetchone()["raw_text_enc"]
    with pytest.raises(Exception, match="authentication failed"):
        TranscriptCipher.from_env().decrypt(blob, "acme/call")


def test_broken_api_key_fails_transcription_then_reprocess_resumes(
        settings, call, worker_conn, user_conn):
    ingest_manifest(worker_conn, call)
    broken = GroqSTT("gsk_broken_key", transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}})))
    validations = Counter()

    def counting_validation(ctx):
        validations[ctx.job.recording_id] += 1
        return build_handlers()["VALIDATION"](ctx)

    run(settings, call, handlers(broken, VALIDATION=counting_validation))
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "stt_auth_error")
    assert "HTTP 401" in r["error_message"] and "Invalid API Key" in r["error_message"]
    assert "channel 1/2" in r["error_message"] and "groq/whisper-large-v3" in r["error_message"]
    assert "gsk_broken_key" not in r["error_message"]
    with tenant_tx(user_conn, "harbor"):
        assert user_conn.execute("SELECT count(*) AS n FROM transcripts").fetchone()["n"] == 0

    # Key fixed -> one command resumes at TRANSCRIPTION; VALIDATION is not repeated.
    assert queue.reprocess(worker_conn) == [("harbor", "call", "TRANSCRIPTION")]
    run(settings, call, handlers(FakeSTT(), VALIDATION=counting_validation))
    assert recording(user_conn)["status"] == "COMPLETED"
    assert validations["call"] == 1


def test_network_timeouts_are_retried_then_fail_with_details(
        settings, tmp_path, make_manifest, worker_conn, user_conn, admin_conn):
    write_wav(tmp_path / "audio" / "call.wav", seconds=2)
    m = make_manifest([{"id": "call", "tenant_id": "harbor", "file": "audio/call.wav",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    ingest_manifest(worker_conn, m, max_attempts=3)
    calls = Counter()

    def timeout(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("connect timed out", request=request)

    stt = GroqSTT("gsk_ok", transport=httpx.MockTransport(timeout), timeout=2)
    run(settings, m, handlers(stt), admin_conn=admin_conn, rounds=4)
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "stt_timeout")
    assert "gave up after 3 attempts" in r["error_message"]
    assert r["retry_count"] == 2
    assert calls["n"] == 3   # channel 1 failed on each attempt


def test_transient_stt_error_recovers(settings, call, worker_conn, user_conn, admin_conn):
    ingest_manifest(worker_conn, call)
    real = FakeSTT()
    state = {"fail": 1}

    class FlakySTT(FakeSTT):
        def transcribe(self, wav, filename, language):
            if state["fail"]:
                state["fail"] -= 1
                raise queue_error()
            return real.transcribe(wav, filename, language)

    from app.errors import TransientError

    def queue_error():
        return TransientError("stt_server_error", "Groq HTTP 503")

    run(settings, call, handlers(FlakySTT()), admin_conn=admin_conn, rounds=3)
    r = recording(user_conn)
    assert r["status"] == "COMPLETED" and r["retry_count"] == 1 and r["error_code"] is None


def test_masking_failure_is_reported_and_reprocess_does_not_retranscribe(
        settings, call, worker_conn, user_conn, monkeypatch):
    ingest_manifest(worker_conn, call)
    stt = FakeSTT()
    import app.steps as steps

    def broken_mask(text, **kwargs):
        raise RuntimeError("regex engine exploded")

    monkeypatch.setattr(steps, "mask_text", broken_mask)
    run(settings, call, handlers(stt))
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "MASKING", "masking_error")
    assert "RuntimeError: regex engine exploded" in r["error_message"]
    assert stt.calls == 2

    monkeypatch.undo()
    queue.reprocess(worker_conn, recording_id="call")
    run(settings, call, handlers(stt))
    assert recording(user_conn)["status"] == "COMPLETED"
    assert stt.calls == 2          # transcription was not repeated


def test_missing_encryption_key_is_a_clear_failure(
        settings, call, worker_conn, user_conn, monkeypatch, tmp_path):
    ingest_manifest(worker_conn, call)
    monkeypatch.delenv("TRANSCRIPT_KEY")
    monkeypatch.setenv("TRANSCRIPT_KEY_FILE", str(tmp_path / "missing.key"))
    stt = FakeSTT()
    hs = build_handlers(stt=stt)
    hs["CRM"] = ok
    run(settings, call, hs)
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "encryption_key_missing")
    assert stt.calls == 0   # no STT quota spent before we know we can store the result


def test_empty_transcript_is_a_failure_not_a_silent_success(
        settings, call, worker_conn, user_conn):
    ingest_manifest(worker_conn, call)

    class Silent(FakeSTT):
        def transcribe(self, wav, filename, language):
            return ChannelTranscript([], "", "tr", 2.0)

    run(settings, call, handlers(Silent()))
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "empty_transcript")


def test_transcripts_stay_tenant_isolated(settings, tmp_path, make_manifest, worker_conn,
                                          user_conn):
    write_wav(tmp_path / "audio" / "a.wav", seconds=2, freq=300)
    write_wav(tmp_path / "audio" / "b.wav", seconds=2, freq=500)
    m = make_manifest([
        {"id": "call", "tenant_id": "harbor", "file": "audio/a.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
        {"id": "call", "tenant_id": "acme", "file": "audio/b.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
    ])
    ingest_manifest(worker_conn, m)
    run(settings, m, handlers(FakeSTT()))
    for tenant in ("harbor", "acme"):
        with tenant_tx(user_conn, tenant):
            rows = user_conn.execute("SELECT tenant_id FROM transcripts").fetchall()
        assert [r["tenant_id"] for r in rows] == [tenant]


# ------------------------------------------------ provider rate limits (HTTP 429)
# Real run: Groq free tier = 20 requests/min, 38 requests needed; three recordings used up
# max_attempts on 429s alone and had to be reprocessed by hand.

class RateLimitedSTT(FakeSTT):
    def __init__(self, limited: int):
        super().__init__()
        self.limited = limited

    def transcribe(self, wav, filename, language):
        from app.errors import TransientError
        if self.limited > 0:
            self.limited -= 1
            raise TransientError("stt_rate_limited", "Groq HTTP 429", retry_after=0)
        return super().transcribe(wav, filename, language)


def test_rate_limit_waits_do_not_use_up_attempts(settings, tmp_path, make_manifest,
                                                  worker_conn, user_conn, admin_conn):
    write_wav(tmp_path / "audio" / "call.wav", seconds=2)
    m = make_manifest([{"id": "call", "tenant_id": "harbor", "file": "audio/call.wav",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    ingest_manifest(worker_conn, m, max_attempts=2)
    stt = RateLimitedSTT(limited=6)                  # 6 x 429 > max_attempts
    run(settings, m, handlers(stt), admin_conn=admin_conn, rounds=10)

    r = recording(user_conn)
    assert r["status"] == "COMPLETED" and r["error_code"] is None
    n = admin_conn.execute("SELECT count(*) AS n FROM recording_events"
                           " WHERE event = 'throttled'").fetchone()["n"]
    assert n == 6


def test_rate_limit_waits_are_capped(settings, tmp_path, make_manifest, worker_conn,
                                     user_conn, admin_conn, monkeypatch):
    """A quota that never comes back (daily limit) must still end in a visible FAILED."""
    monkeypatch.setattr(queue, "MAX_THROTTLE_WAITS", 3)
    write_wav(tmp_path / "audio" / "call.wav", seconds=2)
    m = make_manifest([{"id": "call", "tenant_id": "harbor", "file": "audio/call.wav",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    ingest_manifest(worker_conn, m, max_attempts=2)
    run(settings, m, handlers(RateLimitedSTT(limited=10_000)), admin_conn=admin_conn,
        rounds=12)
    r = recording(user_conn)
    assert (r["status"], r["failed_step"], r["error_code"]) == \
        ("FAILED", "TRANSCRIPTION", "stt_rate_limited")
    assert admin_conn.execute("SELECT count(*) AS n FROM recording_events"
                              " WHERE event = 'throttled'").fetchone()["n"] == 3
