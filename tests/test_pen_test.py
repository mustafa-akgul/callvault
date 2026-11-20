"""Zero-trust / penetration tests: try to cross the tenant boundary the way an attacker
or a careless change would, and prove each attempt is stopped.

Trust boundary (see DECISIONS ADR-30): the tenant is derived from an API key in the
application layer; PostgreSQL enforces it with FORCE RLS on every query. Holding the
app_user DATABASE PASSWORD is outside that boundary (it is a server-side secret);
test_residual_risk_* documents exactly what such a holder can and cannot do.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg import errors

from app.crypto import TranscriptCipher
from app.db import tenant_tx
from app.ingest import ingest_manifest
from app.steps import StepContext, build_handlers
from app.tenant_api import get_transcript, list_actions, list_recordings
from app.tenant_auth import AuthError, authenticate, hash_key, issue_key
from tests.test_transcription_pipeline import FakeSTT, run
from tests.wavs import write_wav


@pytest.fixture
def world(settings, tmp_path, make_manifest, worker_conn):
    """Two tenants with the SAME recording id, both fully transcribed and masked."""
    write_wav(tmp_path / "audio" / "a.wav", seconds=2, freq=300)
    write_wav(tmp_path / "audio" / "b.wav", seconds=2, freq=500)
    m = make_manifest([
        {"id": "call", "tenant_id": "harbor", "file": "audio/a.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
        {"id": "call", "tenant_id": "acme", "file": "audio/b.wav",
         "recorded_at": "2026-10-30T09:00:00+03:00"},
    ])
    ingest_manifest(worker_conn, m)
    hs = build_handlers(stt=FakeSTT(), cipher=TranscriptCipher.from_env())
    hs["CRM"] = lambda ctx: __import__("app.steps", fromlist=["StepResult"]).StepResult()
    run(settings, m, hs)
    return {"harbor": issue_key(worker_conn, "harbor"), "acme": issue_key(worker_conn, "acme"),
            "manifest": m}


# ------------------------------------------------------------ authentication

def test_key_maps_to_exactly_one_tenant(world, user_conn):
    assert authenticate(user_conn, world["harbor"]) == "harbor"
    assert authenticate(user_conn, world["acme"]) == "acme"


@pytest.mark.parametrize("key", [None, "", "not-a-key", "cv_forged" + "A" * 43,
                                 "cv_' OR '1'='1", "cv_" + "x" * 300])
def test_forged_or_malformed_keys_are_rejected(world, user_conn, key):
    with pytest.raises(AuthError):
        authenticate(user_conn, key)


def test_header_spoofing_claimed_tenant_must_match_key(world, user_conn):
    """harbor's valid key + "I am acme" (an X-Tenant-Id header / --tenant flag) -> refused."""
    with pytest.raises(AuthError, match="does not match"):
        authenticate(user_conn, world["harbor"], claimed_tenant="acme")


def test_header_spoofing_via_cli(world, capsys, monkeypatch):
    from app.cli import main
    monkeypatch.setenv("CALLVAULT_API_KEY", world["harbor"])
    assert main(["list-recordings", "--tenant", "acme"]) == 2
    assert "does not match" in capsys.readouterr().out


def test_revoked_key_stops_working(world, user_conn, admin_conn):
    admin_conn.execute("UPDATE api_keys SET revoked_at = now() WHERE key_hash = %s",
                       (hash_key(world["harbor"]),))
    with pytest.raises(AuthError, match="revoked"):
        authenticate(user_conn, world["harbor"])


def test_keys_cannot_be_enumerated(world, user_conn, worker_conn):
    # Without presenting a key's hash, no row is visible: no listing of tenants or keys.
    for conn in (user_conn, worker_conn):
        assert conn.execute("SELECT * FROM api_keys").fetchall() == []
        with tenant_tx(conn, "harbor"):
            assert conn.execute("SELECT * FROM api_keys").fetchall() == []


def test_plaintext_keys_are_not_stored(world, admin_conn):
    rows = admin_conn.execute("SELECT key_hash, tenant_id FROM api_keys").fetchall()
    assert len(rows) == 2
    blob = json.dumps(rows, default=str)
    assert world["harbor"] not in blob and world["acme"] not in blob


def test_key_cannot_be_issued_for_another_tenant(world, worker_conn):
    with pytest.raises(errors.InsufficientPrivilege):
        with tenant_tx(worker_conn, "harbor"):
            worker_conn.execute("INSERT INTO api_keys (key_hash, tenant_id) VALUES (%s, 'acme')",
                                (hash_key("cv_attacker"),))


# ------------------------------------------------------------- SQL injection

@pytest.mark.parametrize("payload", [
    "call' OR '1'='1",
    "call'; SET app.tenant_id = 'acme'; --",
    "call' UNION SELECT id, status, masked_text, NULL, NULL, NULL, NULL, NULL"
    " FROM transcripts --",
    "%",
])
def test_sql_injection_in_recording_id(world, user_conn, payload):
    assert get_transcript(user_conn, "harbor", payload) is None
    assert list_recordings(user_conn, "harbor", payload) == []


def test_sql_injection_in_tenant_context(world, user_conn):
    with tenant_tx(user_conn, "harbor' OR tenant_id = 'acme") as conn:
        assert conn.execute("SELECT * FROM recordings").fetchall() == []


def test_injection_cannot_change_context_mid_transaction(world, user_conn):
    with tenant_tx(user_conn, "harbor"):
        with pytest.raises(psycopg.Error):
            user_conn.execute("SELECT * FROM recordings WHERE id = %s; SET LOCAL app.tenant_id"
                              " = 'acme'", ("call",))


def test_path_traversal_in_manifest_is_rejected(settings, tmp_path, make_manifest, worker_conn,
                                                user_conn):
    m = make_manifest([{"id": "evil", "tenant_id": "harbor", "file": "../../../etc/passwd",
                        "recorded_at": "2026-10-30T09:00:00+03:00"}])
    ingest_manifest(worker_conn, m)
    run(settings, m, build_handlers(stt=FakeSTT(), cipher=TranscriptCipher.from_env()))
    r = list_recordings(user_conn, "harbor", "evil")[0]
    assert (r["status"], r["error_code"]) == ("FAILED", "bad_path")


# ---------------------------------------------------- database role abuse

@pytest.mark.parametrize("sql, err", [
    ("SET ROLE app_owner", errors.InsufficientPrivilege),
    ("SET ROLE postgres", errors.InsufficientPrivilege),
    ("ALTER TABLE recordings DISABLE ROW LEVEL SECURITY", errors.InsufficientPrivilege),
    ("ALTER TABLE recordings NO FORCE ROW LEVEL SECURITY", errors.InsufficientPrivilege),
    ("DROP POLICY tenant_isolation ON recordings", errors.InsufficientPrivilege),
    ("CREATE POLICY open ON recordings USING (true)", errors.InsufficientPrivilege),
    ("CREATE FUNCTION f() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'select 1'",
     errors.InsufficientPrivilege),
    ("CREATE TABLE stash (x text)", errors.InsufficientPrivilege),
    ("SELECT * FROM jobs", errors.InsufficientPrivilege),
    ("SELECT raw_text_enc FROM transcripts", errors.InsufficientPrivilege),
    ("DELETE FROM recordings", errors.InsufficientPrivilege),
    ("UPDATE recordings SET status = 'COMPLETED'", errors.InsufficientPrivilege),
    ("TRUNCATE recordings", errors.InsufficientPrivilege),
    ("GRANT SELECT ON transcripts TO PUBLIC", errors.InsufficientPrivilege),
])
def test_read_role_cannot_escalate(world, user_conn, sql, err):
    with pytest.raises(err):
        with tenant_tx(user_conn, "harbor"):
            user_conn.execute(sql)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE recordings DISABLE ROW LEVEL SECURITY",
    "DELETE FROM recordings",
    "CREATE TABLE stash (x text)",
    "SET ROLE app_owner",
])
def test_worker_role_cannot_escalate(world, worker_conn, sql):
    with pytest.raises(errors.InsufficientPrivilege):
        with tenant_tx(worker_conn, "harbor"):
            worker_conn.execute(sql)


# ------------------------------------------------- cross-tenant decryption

def test_other_tenants_ciphertext_never_decrypts(world, admin_conn):
    cipher = TranscriptCipher.from_env()
    blob = admin_conn.execute(
        "SELECT raw_text_enc FROM transcripts WHERE tenant_id = 'harbor'").fetchone()["raw_text_enc"]
    assert json.loads(cipher.decrypt(blob, "harbor/call"))["text"]
    with pytest.raises(Exception, match="authentication failed"):
        cipher.decrypt(blob, "acme/call")


def test_planted_ciphertext_fails_closed_in_the_pipeline(world, settings, admin_conn,
                                                         worker_conn, user_conn):
    """An attacker with write access copies harbor's raw transcript into acme's row and
    re-runs masking for acme, hoping to get harbor's text masked into acme's view."""
    admin_conn.execute(
        "UPDATE transcripts SET raw_text_enc = (SELECT raw_text_enc FROM transcripts"
        " WHERE tenant_id = 'harbor') WHERE tenant_id = 'acme'")
    from app import queue
    admin_conn.execute("UPDATE jobs SET step = 'MASKING', state = 'queued', attempts = 0"
                       " WHERE tenant_id = 'acme'")
    admin_conn.execute("UPDATE recordings SET status = 'MASKING' WHERE tenant_id = 'acme'")
    job = queue.claim(worker_conn, "attacker", 30)
    with tenant_tx(worker_conn, "acme"):
        rec = worker_conn.execute("SELECT * FROM recordings").fetchone()
    handler = build_handlers(cipher=TranscriptCipher.from_env())["MASKING"]
    with pytest.raises(Exception) as exc:
        handler(StepContext(worker_conn, job, rec, world["manifest"].parent))
    assert getattr(exc.value, "code", None) == "decrypt_failed"


def test_wrong_key_cannot_decrypt(world, admin_conn):
    blob = admin_conn.execute("SELECT raw_text_enc FROM transcripts LIMIT 1").fetchone()
    other = TranscriptCipher(b"\x01" * 32)
    with pytest.raises(Exception, match="authentication failed"):
        other.decrypt(blob["raw_text_enc"], "harbor/call")


# ----------------------------------------------------------- residual risk

def test_residual_risk_db_credentials_allow_context_switch_but_not_raw_data(world, user_conn):
    """DOCUMENTED, NOT A BYPASS OF THE APP: whoever holds the app_user database password
    can choose any tenant context (RLS trusts the session variable). That password is a
    server-side secret, never given to tenants. Even then: read-only, masked text only,
    no raw transcripts, no queue, no deletes (asserted above)."""
    with tenant_tx(user_conn, "acme"):
        rows = user_conn.execute("SELECT masked_text FROM transcripts").fetchall()
    assert rows and all("Ayşe" not in r["masked_text"] for r in rows)


# --------------------------------------------- application read functions

def test_read_functions_only_return_own_rows(world, user_conn):
    for tenant in ("harbor", "acme"):
        assert [r["id"] for r in list_recordings(user_conn, tenant)] == ["call"]
        assert list_actions(user_conn, tenant) == []          # CRM was stubbed out
        t = get_transcript(user_conn, tenant, "call")
        assert t["masked_text"] and "[İSİM MASKELENDİ]" in t["masked_text"]
