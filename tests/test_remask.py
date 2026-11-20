"""`remask`: re-apply the current masking rules to recordings that are already COMPLETED.

Scenario from the first real run: transcripts were masked by older rules that missed
speech-to-text e-mail shapes, and the leaked text was copied into the extraction and the
CRM action payloads. Values below are synthetic.
"""

from __future__ import annotations

import json

import pytest
from psycopg.types.json import Jsonb

from app.crypto import TranscriptCipher
from app.ingest import ingest_manifest
from app.remask import remask
from app.steps import build_handlers
from tests.test_transcription_pipeline import FakeSTT, ok, run
from tests.wavs import write_wav

LEAK = "ali.at.ornek.ai"
EMAIL = "[E-POSTA MASKELENDİ]"


class MailSTT(FakeSTT):
    """The customer spells an address the way Whisper writes it."""

    def transcribe(self, wav, filename, language):
        t = super().transcribe(wav, filename, language)
        if filename.endswith("_ch1.wav"):
            t.segments[-1].text = f"mailim {LEAK} olsun"
        return t


def _make_leaky(admin_conn, tenant="harbor", rid="call", status="SUCCEEDED"):
    """Put the recording into the state the old masking rules left behind."""
    admin_conn.execute(
        "UPDATE transcripts SET masked_text = %s, pii_summary = %s, extraction = %s"
        " WHERE tenant_id = %s AND recording_id = %s",
        (f"Müşteri: mailim {LEAK} olsun", Jsonb({"NAME": 3}),
         Jsonb({"meeting": None, "complaint_summary": f"Müşteri şikayeti: bana {LEAK} yazın"}),
         tenant, rid))
    admin_conn.execute(
        "INSERT INTO crm_actions (tenant_id, recording_id, action_type, status,"
        " request_payload, response_payload, external_id) VALUES (%s, %s, 'complaint', %s,"
        " %s, %s, 'cp_1')",
        (tenant, rid, status,
         Jsonb({"call_id": rid, "summary": f"Müşteri şikayeti: bana {LEAK} yazın"}),
         Jsonb({"id": "cp_1", "call_id": rid, "summary": f"Müşteri şikayeti: bana {LEAK} yazın"})))


def _process(settings, manifest, worker_conn):
    ingest_manifest(worker_conn, manifest)
    hs = build_handlers(stt=MailSTT(), cipher=TranscriptCipher.from_env())
    hs["CRM"] = ok
    run(settings, manifest, hs)


@pytest.fixture
def done(settings, tmp_path, make_manifest, worker_conn):
    write_wav(tmp_path / "audio" / "a.wav", seconds=2, freq=300)
    m = make_manifest([{"id": "call", "tenant_id": "harbor", "file": "audio/a.wav",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    _process(settings, m, worker_conn)
    return m


def _stored(admin_conn, rid="call", tenant="harbor"):
    t = admin_conn.execute("SELECT masked_text, segments, pii_summary, extraction FROM transcripts"
                           " WHERE recording_id = %s AND tenant_id = %s", (rid, tenant)).fetchone()
    a = admin_conn.execute("SELECT request_payload, response_payload FROM crm_actions"
                           " WHERE recording_id = %s AND tenant_id = %s", (rid, tenant)).fetchone()
    return t, a


def test_remask_scrubs_transcript_extraction_and_crm_copies(done, worker_conn, admin_conn):
    _make_leaky(admin_conn)
    report = remask(worker_conn, TranscriptCipher.from_env())

    assert [(r.tenant_id, r.recording_id, r.changed) for r in report] == [("harbor", "call", True)]
    assert report[0].before.get("EMAIL", 0) == 0 and report[0].after["EMAIL"] == 1
    assert report[0].actions_scrubbed == 1
    t, a = _stored(admin_conn)
    everything = json.dumps([t, a], ensure_ascii=False, default=str)
    assert LEAK not in everything
    assert f"mailim {EMAIL} olsun" in t["masked_text"]
    assert t["pii_summary"]["EMAIL"] == 1
    assert EMAIL in t["extraction"]["complaint_summary"]
    assert EMAIL in a["request_payload"]["summary"] and EMAIL in a["response_payload"]["summary"]
    assert a["request_payload"]["call_id"] == "call"          # non-text fields untouched


def test_remask_is_audited_and_idempotent(done, worker_conn, admin_conn):
    _make_leaky(admin_conn)
    remask(worker_conn, TranscriptCipher.from_env())
    events = admin_conn.execute("SELECT detail FROM recording_events"
                                " WHERE event = 'remasked'").fetchall()
    assert len(events) == 1 and "EMAIL" in events[0]["detail"]
    assert LEAK not in events[0]["detail"]

    again = remask(worker_conn, TranscriptCipher.from_env())
    assert [r.changed for r in again] == [False]
    assert admin_conn.execute("SELECT count(*) AS n FROM recording_events"
                              " WHERE event = 'remasked'").fetchone()["n"] == 1


def test_action_that_may_still_be_sent_keeps_its_body(done, worker_conn, admin_conn):
    """A FAILED action is resent byte-for-byte on reprocess (same Idempotency-Key); changing
    its body would make the CRM answer 422. It is reported, not rewritten."""
    _make_leaky(admin_conn, status="FAILED")
    report = remask(worker_conn, TranscriptCipher.from_env())
    assert report[0].actions_kept == 1 and report[0].actions_scrubbed == 0
    _, a = _stored(admin_conn)
    assert LEAK in a["request_payload"]["summary"]


def test_only_completed_recordings_are_touched(done, worker_conn, admin_conn):
    _make_leaky(admin_conn)
    admin_conn.execute("UPDATE recordings SET status = 'PROCESSING_CRM'")
    assert remask(worker_conn, TranscriptCipher.from_env()) == []
    t, _ = _stored(admin_conn)
    assert LEAK in t["masked_text"]


def test_remask_respects_tenant_filter(settings, tmp_path, make_manifest, worker_conn,
                                       admin_conn):
    write_wav(tmp_path / "audio" / "a.wav", seconds=2, freq=300)
    write_wav(tmp_path / "audio" / "b.wav", seconds=2, freq=500)
    m = make_manifest([
        {"id": "call", "tenant_id": "harbor", "file": "audio/a.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
        {"id": "call", "tenant_id": "acme", "file": "audio/b.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
    ])
    _process(settings, m, worker_conn)
    for tenant in ("harbor", "acme"):
        _make_leaky(admin_conn, tenant=tenant)

    report = remask(worker_conn, TranscriptCipher.from_env(), tenant_id="acme")
    assert [(r.tenant_id, r.changed) for r in report] == [("acme", True)]
    assert LEAK not in _stored(admin_conn, tenant="acme")[0]["masked_text"]
    assert LEAK in _stored(admin_conn, tenant="harbor")[0]["masked_text"]


def test_read_role_cannot_run_remask(done, user_conn):
    with pytest.raises(Exception):
        remask(user_conn, TranscriptCipher.from_env())


def test_cli_remask(done, admin_conn, capsys):
    from app.cli import main
    _make_leaky(admin_conn)
    assert main(["remask"]) == 0
    out = capsys.readouterr().out
    assert "harbor/call" in out and "EMAIL 0 -> 1" in out
    assert LEAK not in out
