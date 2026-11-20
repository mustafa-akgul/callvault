"""Re-apply the current masking rules to recordings that are already COMPLETED.

Why this exists: masking rules improve after a recording is done (the first real Groq run
showed spoken e-mail shapes the rules did not know). `reprocess` only re-queues FAILED
recordings, so finished ones would keep the old masked text forever.

What it rewrites, per COMPLETED recording, in one transaction:
  - transcripts.masked_text / segments / pii_summary, from the encrypted raw transcript;
  - free-text copies of that masked text: transcripts.extraction, and crm_actions
    requested_text / request_payload / response_payload ("summary", "text" keys only).

What it deliberately does NOT do:
  - call the CRM. A record already created there keeps whatever it was sent; the result
    lists those CRM ids so a person can correct them in the CRM.
  - rewrite an action that may still be sent (PENDING / IN_FLIGHT / FAILED): its body is
    resent byte-for-byte under the same Idempotency-Key, and a changed body is a 422.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from app.crypto import DecryptionFailed, TranscriptCipher
from app.db import set_tenant
from app.pii import mask_text
from app.queue import _event, _lock_recording
from app.steps import mask_transcript

TEXT_KEYS = {"summary", "complaint_summary", "text"}
FINAL_ACTION = ("SUCCEEDED", "NEEDS_FOLLOWUP")


@dataclass
class RemaskResult:
    tenant_id: str
    recording_id: str
    changed: bool
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    actions_scrubbed: int = 0
    actions_kept: int = 0               # not final: may be resent, body left as is
    sent_to_crm: list[str] = field(default_factory=list)   # CRM ids whose copy we rewrote
    error: str | None = None


def _scrub(value):
    """Re-mask string values under free-text keys; leave ids, slots and times alone."""
    if isinstance(value, dict):
        return {k: (mask_text(v).text if k in TEXT_KEYS and isinstance(v, str) else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _jsonb(value):
    return Jsonb(value) if value is not None else None


def _remask_one(conn, cipher, tenant_id: str, recording_id: str) -> RemaskResult | None:
    with conn.transaction():
        set_tenant(conn, tenant_id)
        if _lock_recording(conn, tenant_id, recording_id)["status"] != "COMPLETED":
            return None                       # a worker owns it, or it never finished
        t = conn.execute(
            "SELECT raw_text_enc, masked_text, pii_summary, extraction FROM transcripts"
            " WHERE tenant_id = %s AND recording_id = %s",
            (tenant_id, recording_id),
        ).fetchone()
        if t is None or t["raw_text_enc"] is None:
            return None
        res = RemaskResult(tenant_id, recording_id, changed=False,
                           before=dict(t["pii_summary"] or {}))
        try:
            doc = json.loads(cipher.decrypt(t["raw_text_enc"], f"{tenant_id}/{recording_id}"))
        except DecryptionFailed as exc:
            res.error = f"decrypt_failed: {exc}"
            return res
        masked_text, turns, counts = mask_transcript(doc)
        res.after = dict(counts)

        if masked_text != t["masked_text"]:
            res.changed = True
            conn.execute(
                "UPDATE transcripts SET masked_text = %s, segments = %s, pii_summary = %s,"
                " masked_at = now(), updated_at = now()"
                " WHERE tenant_id = %s AND recording_id = %s",
                (masked_text, Jsonb(turns), Jsonb(dict(counts)), tenant_id, recording_id),
            )
        if t["extraction"] is not None:
            scrubbed = _scrub(t["extraction"])
            if scrubbed != t["extraction"]:
                res.changed = True
                conn.execute("UPDATE transcripts SET extraction = %s"
                             " WHERE tenant_id = %s AND recording_id = %s",
                             (Jsonb(scrubbed), tenant_id, recording_id))

        actions = conn.execute(
            "SELECT id, status, external_id, requested_text, request_payload, response_payload"
            " FROM crm_actions WHERE tenant_id = %s AND recording_id = %s ORDER BY id",
            (tenant_id, recording_id),
        ).fetchall()
        for a in actions:
            new = {
                "requested_text": (mask_text(a["requested_text"]).text
                                   if a["requested_text"] else a["requested_text"]),
                "request_payload": _scrub(a["request_payload"]),
                "response_payload": _scrub(a["response_payload"]),
            }
            if all(new[k] == a[k] for k in new):
                continue
            if a["status"] not in FINAL_ACTION:
                res.actions_kept += 1
                continue
            conn.execute(
                "UPDATE crm_actions SET requested_text = %s, request_payload = %s,"
                " response_payload = %s, updated_at = now() WHERE id = %s",
                (new["requested_text"], _jsonb(new["request_payload"]),
                 _jsonb(new["response_payload"]), a["id"]),
            )
            res.changed = True
            res.actions_scrubbed += 1
            if a["external_id"]:
                res.sent_to_crm.append(a["external_id"])

        if res.changed:
            _event(conn, tenant_id, recording_id, "remasked", step="MASKING",
                   detail=f"pii {res.before} -> {res.after}; crm actions rewritten "
                          f"{res.actions_scrubbed}, kept {res.actions_kept}")
        return res


def remask(conn: psycopg.Connection, cipher: TranscriptCipher, tenant_id: str | None = None,
           recording_id: str | None = None) -> list[RemaskResult]:
    """Re-mask COMPLETED recordings (optionally one tenant / one recording)."""
    where, params = ["state = 'done'"], []
    if tenant_id:
        where.append("tenant_id = %s")
        params.append(tenant_id)
    if recording_id:
        where.append("recording_id = %s")
        params.append(recording_id)
    targets = conn.execute(
        f"SELECT tenant_id, recording_id FROM jobs WHERE {' AND '.join(where)}"
        " ORDER BY tenant_id, recording_id", params).fetchall()
    results = []
    for row in targets:
        r = _remask_one(conn, cipher, row["tenant_id"], row["recording_id"])
        if r is not None:
            results.append(r)
    return results
