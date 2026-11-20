"""Tenant isolation, enforced by PostgreSQL itself.

Each test tries one specific way of breaking isolation by mistake. Both tenants have a
recording with the SAME id (rec_001) on purpose: ids alone never identify data.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from app.db import tenant_tx

TENANT_TABLES = ("tenants", "recordings", "transcripts", "crm_actions", "recording_events")


def seed(conn, tenant: str, secret: str) -> None:
    with tenant_tx(conn, tenant):
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, tenant.title()))
        for rec in ("rec_001", f"{tenant}_only"):
            conn.execute(
                "INSERT INTO recordings (tenant_id, id, file_path, recorded_at)"
                " VALUES (%s, %s, %s, '2026-10-30T09:00:00+03:00')",
                (tenant, rec, f"recordings/{rec}.wav"),
            )
            conn.execute(
                "INSERT INTO jobs (tenant_id, recording_id, step) VALUES (%s, %s, 'VALIDATION')",
                (tenant, rec),
            )
        conn.execute(
            "INSERT INTO transcripts (tenant_id, recording_id, provider, raw_text_enc,"
            " masked_text) VALUES (%s, 'rec_001', 'test', '\\x00', %s)",
            (tenant, f"{secret} transcript"),
        )
        conn.execute(
            "INSERT INTO crm_actions (tenant_id, recording_id, action_type)"
            " VALUES (%s, 'rec_001', 'complaint')",
            (tenant,),
        )
        conn.execute(
            "INSERT INTO recording_events (tenant_id, recording_id, event)"
            " VALUES (%s, 'rec_001', 'ingested')",
            (tenant,),
        )


@pytest.fixture
def two_tenants(worker_conn):
    seed(worker_conn, "harbor", "HARBOR-SECRET")
    seed(worker_conn, "acme", "ACME-SECRET")


# ------------------------------------------------------------------ fail closed

@pytest.mark.parametrize("table", TENANT_TABLES)
def test_query_without_tenant_context_is_an_error(two_tenants, user_conn, table):
    with pytest.raises(errors.InsufficientPrivilege, match="tenant context is not set"):
        user_conn.execute(f"SELECT * FROM {table}").fetchall()


def test_no_context_is_an_error_for_worker_role_too(two_tenants, worker_conn):
    with pytest.raises(errors.InsufficientPrivilege, match="tenant context is not set"):
        worker_conn.execute("SELECT * FROM recordings").fetchall()


def test_context_does_not_survive_its_transaction(two_tenants, user_conn):
    """Pooled-connection trap: after SET LOCAL ends, the GUC is '' (not unset).
    A naive policy would then silently return nothing; ours must still error."""
    with tenant_tx(user_conn, "harbor"):
        assert user_conn.execute("SELECT count(*) AS n FROM recordings").fetchone()["n"] == 2
    assert user_conn.execute("SELECT current_setting('app.tenant_id', true) AS v"
                             ).fetchone()["v"] == ""
    with pytest.raises(errors.InsufficientPrivilege):
        user_conn.execute("SELECT * FROM recordings").fetchall()


def test_table_owner_is_subject_to_rls(two_tenants, owner_conn):
    """FORCE ROW LEVEL SECURITY: connecting as the owner by mistake is not a bypass."""
    with pytest.raises(errors.InsufficientPrivilege):
        owner_conn.execute("SELECT * FROM recordings").fetchall()
    with tenant_tx(owner_conn, "acme"):
        ids = {r["tenant_id"] for r in owner_conn.execute("SELECT tenant_id FROM recordings")}
    assert ids == {"acme"}


def test_tenant_tx_cannot_be_nested(user_conn):
    with tenant_tx(user_conn, "harbor"):
        with pytest.raises(RuntimeError, match="nested"):
            with tenant_tx(user_conn, "acme"):
                pass


# --------------------------------------------------------------------- reads

@pytest.mark.parametrize("tenant,other", [("harbor", "acme"), ("acme", "harbor")])
def test_each_table_shows_only_own_rows(two_tenants, user_conn, tenant, other):
    with tenant_tx(user_conn, tenant):
        for table in TENANT_TABLES:
            col = "id" if table == "tenants" else "tenant_id"
            seen = {r[col] for r in user_conn.execute(f"SELECT {col} FROM {table}")}
            assert seen == {tenant}, table
        text = user_conn.execute("SELECT string_agg(masked_text, ' ') AS t FROM transcripts"
                                 ).fetchone()["t"]
        assert other.upper() not in text


def test_lookup_by_other_tenants_id_finds_nothing(two_tenants, user_conn):
    with tenant_tx(user_conn, "harbor"):
        row = user_conn.execute("SELECT * FROM recordings WHERE id = 'acme_only'").fetchone()
        assert row is None
        # Even naming the other tenant explicitly does not help.
        row = user_conn.execute(
            "SELECT masked_text FROM transcripts"
            " WHERE tenant_id = 'acme' AND recording_id = 'rec_001'"
        ).fetchone()
        assert row is None


def test_joins_cannot_reach_other_tenant(two_tenants, user_conn):
    with tenant_tx(user_conn, "acme"):
        rows = user_conn.execute(
            "SELECT r.tenant_id, t.masked_text FROM recordings r"
            " LEFT JOIN transcripts t ON t.recording_id = r.id"
        ).fetchall()
    assert rows and all(r["tenant_id"] == "acme" for r in rows)
    assert all("HARBOR" not in (r["masked_text"] or "") for r in rows)


# --------------------------------------------------------------------- writes

def test_insert_for_another_tenant_is_rejected(two_tenants, worker_conn):
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        with tenant_tx(worker_conn, "acme"):
            worker_conn.execute(
                "INSERT INTO recordings (tenant_id, id, file_path, recorded_at)"
                " VALUES ('harbor', 'injected', 'x.wav', now())"
            )


def test_update_cannot_touch_other_tenant(two_tenants, worker_conn, admin_conn):
    with tenant_tx(worker_conn, "acme"):
        cur = worker_conn.execute("UPDATE recordings SET retry_count = 99 WHERE id = 'rec_001'")
        assert cur.rowcount == 1  # only acme's rec_001
        with pytest.raises(errors.InsufficientPrivilege):
            # Moving a row to another tenant is a WITH CHECK violation.
            worker_conn.execute("UPDATE recordings SET tenant_id = 'harbor' WHERE id = 'acme_only'")
    harbor = admin_conn.execute(
        "SELECT retry_count FROM recordings WHERE tenant_id = 'harbor' AND id = 'rec_001'"
    ).fetchone()
    assert harbor["retry_count"] == 0


def test_cross_tenant_foreign_key_is_impossible(two_tenants, worker_conn):
    """FK checks bypass RLS, so the composite FK is what stops cross-tenant links."""
    with pytest.raises(errors.ForeignKeyViolation):
        with tenant_tx(worker_conn, "acme"):
            worker_conn.execute(
                "INSERT INTO crm_actions (tenant_id, recording_id, action_type)"
                " VALUES ('acme', 'callvault_only', 'booking')"
            )


def test_nobody_can_delete(two_tenants, worker_conn, user_conn):
    for conn in (worker_conn, user_conn):
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            with tenant_tx(conn, "harbor"):
                conn.execute("DELETE FROM recordings WHERE id = 'rec_001'")


def test_read_role_cannot_write_or_see_queue(two_tenants, user_conn):
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        with tenant_tx(user_conn, "harbor"):
            user_conn.execute("UPDATE recordings SET retry_count = 1")
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        with tenant_tx(user_conn, "harbor"):
            user_conn.execute("SELECT * FROM jobs").fetchall()


# --------------------------------------------------- guard rails for the future

def test_every_tenant_table_has_forced_rls(owner_conn):
    """Adding a table with tenant_id and forgetting RLS breaks this test."""
    rows = owner_conn.execute(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND (EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                       AND a.attname = 'tenant_id' AND NOT a.attisdropped)
               OR c.relname = 'tenants')
        """
    ).fetchall()
    names = {r["relname"] for r in rows}
    assert {"tenants", "recordings", "transcripts", "crm_actions", "jobs",
            "recording_events"} <= names
    for r in rows:
        assert r["relrowsecurity"] and r["relforcerowsecurity"] and r["policies"] > 0, r


def test_application_roles_cannot_bypass_rls(owner_conn):
    roles = owner_conn.execute(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles"
        " WHERE rolname IN ('app_owner', 'app_worker', 'app_user')"
    ).fetchall()
    assert len(roles) == 3
    assert not any(r["rolsuper"] or r["rolbypassrls"] for r in roles)
    owners = {r["owner"] for r in owner_conn.execute(
        "SELECT tableowner AS owner FROM pg_tables WHERE schemaname = 'public'")}
    assert owners == {"app_owner"}


def test_no_security_definer_functions(owner_conn):
    """SECURITY DEFINER functions run as their owner and can sidestep RLS."""
    rows = owner_conn.execute(
        "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.prosecdef"
    ).fetchall()
    assert rows == []


# --------------------------------------------------- CRM idempotency keys

def test_idempotency_key_is_tenant_scoped_and_derived(two_tenants, admin_conn):
    keys = {r["idempotency_key"] for r in admin_conn.execute(
        "SELECT idempotency_key FROM crm_actions")}
    # Same recording id in two tenants -> two different keys.
    assert keys == {"harbor:complaint:rec_001", "acme:complaint:rec_001"}


def test_idempotency_key_cannot_be_set_by_hand(two_tenants, worker_conn):
    with pytest.raises(psycopg.errors.GeneratedAlways):
        with tenant_tx(worker_conn, "harbor"):
            worker_conn.execute(
                "INSERT INTO crm_actions (tenant_id, recording_id, action_type, idempotency_key)"
                " VALUES ('harbor', 'callvault_only', 'booking', 'acme:booking:rec_001')"
            )


def test_one_action_of_each_type_per_recording(two_tenants, worker_conn):
    with pytest.raises(errors.UniqueViolation):
        with tenant_tx(worker_conn, "harbor"):
            worker_conn.execute(
                "INSERT INTO crm_actions (tenant_id, recording_id, action_type)"
                " VALUES ('harbor', 'rec_001', 'complaint')"
            )
