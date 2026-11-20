"""Read side for a tenant: recordings, masked transcripts, CRM actions, metrics.

Every function takes an already-authenticated tenant id and runs under tenant_tx() on an
app_user connection, so RLS scopes every query even if a function forgot a filter.
Only masked text is ever selected; app_user cannot read the raw column at all.
"""

from __future__ import annotations

import psycopg

from app.db import tenant_tx


def list_recordings(conn: psycopg.Connection, tenant: str,
                    recording_id: str | None = None) -> list[dict]:
    with tenant_tx(conn, tenant):
        return conn.execute(
            "SELECT id, status, current_step, failed_step, retry_count, duplicate_of,"
            " error_code, error_message, recorded_at, updated_at FROM recordings"
            " WHERE (%s::text IS NULL OR id = %s) ORDER BY id",
            (recording_id, recording_id),
        ).fetchall()


def recording_events(conn: psycopg.Connection, tenant: str, recording_id: str) -> list[dict]:
    with tenant_tx(conn, tenant):
        return conn.execute(
            "SELECT at, event, step, from_status, to_status, detail, worker"
            " FROM recording_events WHERE recording_id = %s ORDER BY id",
            (recording_id,),
        ).fetchall()


def get_transcript(conn: psycopg.Connection, tenant: str, recording_id: str) -> dict | None:
    with tenant_tx(conn, tenant):
        return conn.execute(
            "SELECT r.id, r.status, t.masked_text, t.pii_summary, t.stt_meta, t.provider,"
            " t.model, t.masked_at FROM recordings r"
            " LEFT JOIN transcripts t ON t.tenant_id = r.tenant_id AND t.recording_id = r.id"
            " WHERE r.id = %s",
            (recording_id,),
        ).fetchone()


def list_actions(conn: psycopg.Connection, tenant: str) -> list[dict]:
    with tenant_tx(conn, tenant):
        return conn.execute(
            "SELECT recording_id, action_type, status, requested_text, requested_start,"
            " requested_window, booked_slot_start, followup_reason, external_id,"
            " request_payload, last_error, attempts FROM crm_actions"
            " ORDER BY recording_id, action_type"
        ).fetchall()


def metrics(conn: psycopg.Connection, tenant: str) -> dict:
    with tenant_tx(conn, tenant):
        q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
        by_status = {r["status"]: r["n"] for r in q(
            "SELECT status::text, count(*) AS n FROM recordings GROUP BY 1")}
        failed_by_step = {r["step"]: r["n"] for r in q(
            "SELECT failed_step::text AS step, count(*) AS n FROM recordings"
            " WHERE status = 'FAILED' GROUP BY 1")}
        retries = q("SELECT coalesce(sum(retry_count), 0) AS total,"
                    " count(*) FILTER (WHERE retry_count > 0) AS recordings,"
                    " coalesce(max(retry_count), 0) AS max FROM recordings")[0]
        events = {r["event"]: r["n"] for r in q(
            "SELECT event, count(*) AS n FROM recording_events GROUP BY 1")}
        pii = {r["kind"]: int(r["n"]) for r in q(
            "SELECT kv.key AS kind, sum(kv.value::int) AS n FROM transcripts,"
            " jsonb_each_text(coalesce(pii_summary, '{}'::jsonb)) kv GROUP BY 1")}
        actions = {(r["action_type"], r["status"]): r["n"] for r in q(
            "SELECT action_type, status, count(*) AS n FROM crm_actions GROUP BY 1, 2")}
        attempts = q("SELECT count(*) AS attempts, coalesce(sum(sends), 0) AS sends,"
                     " count(*) FILTER (WHERE status = 'CONFLICT') AS conflicts,"
                     " count(*) FILTER (WHERE status = 'ADOPTED') AS adopted"
                     " FROM crm_booking_attempts")[0]
        rules = {r["rule"]: r["n"] for r in q(
            "SELECT substring(request_body->>'note' from 'rule=([a-z_]+)') AS rule,"
            " count(*) AS n FROM crm_booking_attempts WHERE status IN ('SUCCEEDED', 'ADOPTED')"
            " GROUP BY 1")}
    return {"by_status": by_status, "failed_by_step": failed_by_step, "retries": retries,
            "events": events, "pii": pii, "actions": actions, "booking_attempts": attempts,
            "booking_rules": rules, "total": sum(by_status.values())}
