"""PROCESSING_CRM: turn the masked transcript into CRM bookings / complaints, exactly once.

Order of operations (each arrow is a committed, lease-fenced transaction):

  1. plan      extraction -> transcripts.extraction + crm_actions rows (outbox).
               Done once; retries and reprocess reuse the stored plan.
  2. booking   per attempt:
                 reconcile  GET /bookings: if the CRM already has this call's booking
                            (ours, by call_id + tenant tag in the note), adopt it.
                 plan slot  only if there is no attempt in flight: GET /slots,
                            choose_slot(), INSERT attempt (key + body) -> commit
                 send       POST /bookings with the attempt's key and body. Unknown
                            outcome (timeout, dropped connection, 5xx, 429 after
                            retries) -> the attempt stays PLANNED and is resent as is.
                 409        the ONLY way to a new slot: attempt -> CONFLICT, new attempt.
  3. complaint same, with one fixed key and body.

Duplicates are impossible by construction: one PLANNED attempt per booking (partial
unique index), one success per recording (unique index), key derived by the database,
body never rebuilt, and the CRM replays a known key instead of writing again.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date, datetime

import psycopg
from psycopg.types.json import Jsonb

from app.booking import choose_slot
from app.crm import CRMClient
from app.db import tenant_tx
from app.errors import PermanentError, StepError
from app.extraction import extract
from app.queue import owned
from app.steps import StepResult
from app.timeparse_tr import TimeRequest

log = logging.getLogger(__name__)

MAX_SLOT_TRIES = 10
DONE = ("SUCCEEDED", "NEEDS_FOLLOWUP")


def _tag(tenant_id: str) -> str:
    return f"tenant={tenant_id}"


def _request_from_json(d: dict) -> TimeRequest:
    def dt(v):
        return datetime.fromisoformat(v) if v else None

    def d_(v):
        return date.fromisoformat(v) if v else None

    return TimeRequest(text=d.get("text", ""), exact=dt(d.get("exact")),
                       date_from=d_(d.get("date_from")), date_to=d_(d.get("date_to")),
                       weekdays=d.get("weekdays"), hour_from=d.get("hour_from"),
                       hour_to=d.get("hour_to"), notes=d.get("notes") or [])


# ---------------------------------------------------------------------- planning

def plan_actions(ctx) -> None:
    conn, job, rec = ctx.conn, ctx.job, ctx.recording
    with tenant_tx(conn, job.tenant_id):
        t = conn.execute(
            "SELECT segments, extraction FROM transcripts"
            " WHERE tenant_id = %s AND recording_id = %s",
            (job.tenant_id, job.recording_id),
        ).fetchone()
    if t is None or t["segments"] is None:
        raise PermanentError("transcript_not_masked",
                             "no masked transcript; reprocess from TRANSCRIPTION/MASKING",
                             failed_step="CRM")
    if t["extraction"] is not None:
        return  # already planned by an earlier attempt

    ext = extract(t["segments"], rec["recorded_at"])
    summary = {
        "meeting": ext.meeting.to_json() if ext.meeting else None,
        "complaint_summary": ext.complaint_summary,
    }
    with owned(conn, job):
        conn.execute(
            "UPDATE transcripts SET extraction = %s, extracted_at = now()"
            " WHERE tenant_id = %s AND recording_id = %s AND extraction IS NULL",
            (Jsonb(summary), job.tenant_id, job.recording_id),
        )
        if ext.meeting:
            conn.execute(
                "INSERT INTO crm_actions (tenant_id, recording_id, action_type, requested_text,"
                " requested_start, requested_window) VALUES (%s, %s, 'booking', %s, %s, %s)"
                " ON CONFLICT (tenant_id, recording_id, action_type) DO NOTHING",
                (job.tenant_id, job.recording_id, ext.meeting.text, ext.meeting.exact,
                 Jsonb(ext.meeting.to_json())),
            )
        if ext.complaint_summary:
            conn.execute(
                "INSERT INTO crm_actions (tenant_id, recording_id, action_type, request_payload)"
                " VALUES (%s, %s, 'complaint', %s)"
                " ON CONFLICT (tenant_id, recording_id, action_type) DO NOTHING",
                (job.tenant_id, job.recording_id,
                 Jsonb({"call_id": job.recording_id, "summary": ext.complaint_summary})),
            )


def _actions(ctx) -> list[dict]:
    with tenant_tx(ctx.conn, ctx.job.tenant_id):
        return ctx.conn.execute(
            "SELECT * FROM crm_actions WHERE recording_id = %s ORDER BY action_type",
            (ctx.job.recording_id,),
        ).fetchall()


def _set_action(ctx, action_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = %s" for k in fields)
    with owned(ctx.conn, ctx.job):
        ctx.conn.execute(
            f"UPDATE crm_actions SET {cols}, updated_at = now() WHERE id = %s",
            [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
            + [action_id],
        )


# ----------------------------------------------------------------------- booking

def _attempts(ctx, action_id: int) -> list[dict]:
    with tenant_tx(ctx.conn, ctx.job.tenant_id):
        return ctx.conn.execute(
            "SELECT * FROM crm_booking_attempts WHERE action_id = %s ORDER BY id", (action_id,)
        ).fetchall()


def _adopt(ctx, action: dict, booking: dict, planned: dict | None) -> None:
    with owned(ctx.conn, ctx.job):
        if planned is not None and planned["slot_start_text"] == booking["slot_start"]:
            ctx.conn.execute(
                "UPDATE crm_booking_attempts SET status = 'ADOPTED', response = %s,"
                " updated_at = now() WHERE id = %s",
                (Jsonb(booking), planned["id"]),
            )
        elif planned is not None:  # cannot normally happen; keep the record honest
            ctx.conn.execute(
                "UPDATE crm_booking_attempts SET status = 'REJECTED', response = %s,"
                " updated_at = now() WHERE id = %s",
                (Jsonb({"superseded_by_existing_booking": booking}), planned["id"]),
            )
        ctx.conn.execute(
            "UPDATE crm_actions SET status = 'SUCCEEDED', booked_slot_start = %s,"
            " external_id = %s, response_payload = %s, last_error = NULL, updated_at = now()"
            " WHERE id = %s",
            (booking["slot_start"], booking["id"], Jsonb(booking), action["id"]),
        )


def process_booking(ctx, crm: CRMClient, action: dict) -> None:
    job, rec = ctx.job, ctx.recording
    tag = _tag(job.tenant_id)
    req = _request_from_json(action["requested_window"] or {})

    for _ in range(MAX_SLOT_TRIES):
        attempts = _attempts(ctx, action["id"])
        planned = next((a for a in attempts if a["status"] == "PLANNED"), None)

        # Reconcile first: a lost response or a crash after the CRM committed must end
        # here, not in a second booking.
        mine = [b for b in crm.list_bookings()
                if b.get("call_id") == job.recording_id and tag in (b.get("note") or "")]
        if mine:
            _adopt(ctx, action, mine[0], planned)
            return

        if planned is None:
            slots = crm.get_slots()
            conflicts = {a["slot_start_text"] for a in attempts if a["status"] == "CONFLICT"}
            slot, rule = choose_slot(req, slots, rec["recorded_at"], conflicts)
            if slot is None:
                _set_action(ctx, action["id"], status="NEEDS_FOLLOWUP", followup_reason=rule)
                return
            body = {
                "call_id": job.recording_id,
                "slot_start": slot.start,
                "requested_start": req.exact.isoformat() if req.exact else None,
                "note": f"{tag}; rule={rule}",
            }
            with owned(ctx.conn, job):
                planned = ctx.conn.execute(
                    "INSERT INTO crm_booking_attempts (tenant_id, recording_id, action_id,"
                    " slot_start, slot_start_text, request_body)"
                    " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    (job.tenant_id, job.recording_id, action["id"], slot.start, slot.start,
                     Jsonb(body)),
                ).fetchone()
                ctx.conn.execute(
                    "UPDATE crm_actions SET status = 'IN_FLIGHT', attempts = attempts + 1,"
                    " updated_at = now() WHERE id = %s",
                    (action["id"],),
                )

        with owned(ctx.conn, job):
            ctx.conn.execute("UPDATE crm_booking_attempts SET sends = sends + 1,"
                             " updated_at = now() WHERE id = %s", (planned["id"],))
        try:
            resp = crm.create_booking(planned["request_body"], planned["idempotency_key"])
        except PermanentError as exc:
            with owned(ctx.conn, job):
                ctx.conn.execute("UPDATE crm_booking_attempts SET status = 'REJECTED',"
                                 " updated_at = now() WHERE id = %s", (planned["id"],))
            raise

        if resp.status == 409:
            with owned(ctx.conn, job):
                ctx.conn.execute(
                    "UPDATE crm_booking_attempts SET status = 'CONFLICT', http_status = 409,"
                    " response = %s, updated_at = now() WHERE id = %s",
                    (Jsonb(resp.body), planned["id"]),
                )
            log.info("%s/%s: slot %s taken (409), choosing the next one", job.tenant_id,
                     job.recording_id, planned["slot_start_text"])
            continue

        with owned(ctx.conn, job):
            ctx.conn.execute(
                "UPDATE crm_booking_attempts SET status = 'SUCCEEDED', http_status = %s,"
                " response = %s, updated_at = now() WHERE id = %s",
                (resp.status, Jsonb(resp.body), planned["id"]),
            )
            ctx.conn.execute(
                "UPDATE crm_actions SET status = 'SUCCEEDED', booked_slot_start = %s,"
                " external_id = %s, response_payload = %s, request_payload = %s,"
                " last_error = NULL, updated_at = now() WHERE id = %s",
                (resp.body["slot_start"], resp.body.get("id"), Jsonb(resp.body),
                 Jsonb(planned["request_body"]), action["id"]),
            )
        return

    _set_action(ctx, action["id"], status="NEEDS_FOLLOWUP",
                followup_reason=f"every chosen slot was taken ({MAX_SLOT_TRIES} tries)")


# --------------------------------------------------------------------- complaint

def process_complaint(ctx, crm: CRMClient, action: dict) -> None:
    body = action["request_payload"]
    existing = [c for c in crm.list_complaints()
                if c.get("call_id") == body["call_id"] and c.get("summary") == body["summary"]]
    if existing:
        _set_action(ctx, action["id"], status="SUCCEEDED", external_id=existing[0]["id"],
                    response_payload=existing[0], last_error=None)
        return
    with owned(ctx.conn, ctx.job):
        ctx.conn.execute("UPDATE crm_actions SET status = 'IN_FLIGHT', attempts = attempts + 1,"
                         " updated_at = now() WHERE id = %s", (action["id"],))
    resp = crm.create_complaint(body, action["idempotency_key"])
    _set_action(ctx, action["id"], status="SUCCEEDED", external_id=resp.body.get("id"),
                response_payload=resp.body, last_error=None)


# -------------------------------------------------------------------------- step

def make_crm_step(get_crm: Callable[[], CRMClient]):
    def crm_step(ctx):
        plan_actions(ctx)
        crm = get_crm()
        errors: list[StepError] = []
        for action in _actions(ctx):
            if action["status"] in DONE:
                continue
            sub = "CRM_BOOKING" if action["action_type"] == "booking" else "CRM_COMPLAINT"
            try:
                if action["action_type"] == "booking":
                    # One booking at a time per tenant: fewer 409 races, and the session
                    # lock dies with the connection if this worker is killed.
                    ctx.conn.execute("SELECT pg_advisory_lock(hashtext(%s))",
                                     (f"crm-booking:{ctx.job.tenant_id}",))
                    try:
                        process_booking(ctx, crm, action)
                    finally:
                        ctx.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                                         (f"crm-booking:{ctx.job.tenant_id}",))
                else:
                    process_complaint(ctx, crm, action)
            except StepError as exc:
                exc.failed_step = sub
                exc.message = f"[{action['action_type']}] {exc.message}"
                errors.append(exc)
                _set_action(ctx, action["id"], last_error=f"{exc.code}: {exc.message}"[:2000])
        if errors:
            # A permanent problem needs a human; otherwise retry the whole step later
            # (completed actions are skipped, in-flight ones resend the same request).
            raise next((e for e in errors if isinstance(e, PermanentError)), errors[0])
        return StepResult()

    return crm_step


def crm_from_env() -> CRMClient:
    return CRMClient(os.environ.get("CRM_BASE_URL", "http://localhost:8080"),
                     timeout=float(os.environ.get("CRM_TIMEOUT_SECONDS", "20")))

