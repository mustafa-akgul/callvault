"""The tenant CLI and the reprocess command, through app.cli.main()."""

from __future__ import annotations

import pytest

from app.cli import main
from app.crypto import TranscriptCipher
from app.ingest import ingest_manifest
from app.steps import build_handlers
from app.tenant_auth import issue_key
from tests.fake_crm import FakeCRM
from tests.test_crm_pipeline import ScriptedSTT, crm_client, make_calls, run


@pytest.fixture
def processed(settings, tmp_path, make_manifest, worker_conn):
    crm = FakeCRM()
    m = make_calls(tmp_path, make_manifest, "meet_and_complain", "nothing")
    ingest_manifest(worker_conn, m)
    run(settings, m, ScriptedSTT(), crm_client(crm))
    return {"key": issue_key(worker_conn, "harbor"), "manifest": m, "crm": crm}


def cli(capsys, *argv) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_list_recordings(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    code, out = cli(capsys, "list-recordings")
    assert code == 0
    assert "meet_and_complain" in out and "nothing" in out and "COMPLETED" in out


def test_list_recordings_with_history(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    code, out = cli(capsys, "list-recordings", "--recording-id", "nothing", "--events")
    assert code == 0
    for event in ("ingested", "claimed", "step_completed"):
        assert event in out


def test_get_transcript_is_masked(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    code, out = cli(capsys, "get-transcript", "--recording-id", "meet_and_complain")
    assert code == 0
    assert "[İSİM MASKELENDİ]" in out and "Ayşe" not in out and "Yılmaz" not in out
    assert "Temsilci:" in out and "Müşteri:" in out


def test_get_transcript_of_unknown_recording(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    assert cli(capsys, "get-transcript", "--recording-id", "nope")[0] == 1


def test_list_actions_shows_asked_and_booked(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    code, out = cli(capsys, "list-actions")
    assert code == 0
    assert "booking" in out and "complaint" in out and "SUCCEEDED" in out
    assert "2026-11-02T15:00" in out          # asked == booked here
    assert "Müşteri şikayeti" in out


def test_metrics(processed, capsys, monkeypatch):
    monkeypatch.setenv("CALLVAULT_API_KEY", processed["key"])
    code, out = cli(capsys, "metrics")
    assert code == 0
    for text in ("COMPLETED", "PII masked", "NAME", "booking SUCCEEDED", "complaint SUCCEEDED",
                 "booked by rule exact"):
        assert text in out, text


def test_tenant_commands_require_a_key(processed, capsys, monkeypatch):
    monkeypatch.delenv("CALLVAULT_API_KEY", raising=False)
    code, out = cli(capsys, "list-recordings")
    assert code == 2 and "authentication failed" in out


def test_other_tenants_key_sees_nothing_of_harbor(processed, capsys, monkeypatch, worker_conn):
    with __import__("app.db", fromlist=["tenant_tx"]).tenant_tx(worker_conn, "acme"):
        worker_conn.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme')")
    monkeypatch.setenv("CALLVAULT_API_KEY", issue_key(worker_conn, "acme"))
    code, out = cli(capsys, "list-recordings")
    assert code == 0 and "no recordings" in out
    assert cli(capsys, "get-transcript", "--recording-id", "meet_and_complain")[0] == 1


def test_reprocess_requires_a_target(capsys):
    assert cli(capsys, "reprocess")[0] == 2


def test_reprocess_resumes_at_failed_step_and_skips_done_steps(
        settings, tmp_path, make_manifest, worker_conn, admin_conn, capsys, monkeypatch):
    crm = FakeCRM()
    crm.fail("POST", "/bookings", *["503"] * 40)
    m = make_calls(tmp_path, make_manifest, "meet_only")
    ingest_manifest(worker_conn, m, max_attempts=1)
    stt = ScriptedSTT()
    run(settings, m, stt, crm_client(crm, max_attempts=2))
    calls = stt.calls

    code, out = cli(capsys, "reprocess", "--recording-id", "meet_only")
    assert code == 0
    assert "at CRM" in out and "VALIDATION, TRANSCRIPTION, MASKING" in out

    crm.faults.clear()
    run(settings, m, stt, crm_client(crm))
    assert stt.calls == calls                                # nothing before CRM re-ran
    monkeypatch.setenv("CALLVAULT_API_KEY", issue_key(worker_conn, "harbor"))
    assert "COMPLETED" in cli(capsys, "list-recordings")[1]
    assert len(crm.bookings) == 1


def test_reprocess_all_failed(settings, tmp_path, make_manifest, worker_conn, capsys,
                             monkeypatch):
    m = make_calls(tmp_path, make_manifest, "nothing")
    ingest_manifest(worker_conn, m)

    def broken_stt():
        hs = build_handlers(stt=None, cipher=TranscriptCipher.from_env())
        return hs

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("STT_PROVIDER", "groq")
    from app.worker import Worker
    w = Worker(settings.worker_url, audio_root=m.parent, handlers=broken_stt(), poll_seconds=0)
    while w.run_once():
        pass
    w.close()
    # Regression: Rich must not swallow "[...]" in data as markup (error text, masks).
    monkeypatch.setenv("CALLVAULT_API_KEY", issue_key(worker_conn, "harbor"))
    out = cli(capsys, "list-recordings")[1]
    assert "[groq/whisper-large-v3, channel 1/2]" in out

    code, out = cli(capsys, "reprocess", "--all-failed")
    assert code == 0 and "requeued harbor/nothing at TRANSCRIPTION" in out
    assert "kept, not re-run: VALIDATION" in out
