"""Read manifest.json and register every recording + its first job. Idempotent.

Ingest never decides that a file is bad; it records what is there (hash, size) and
queues a VALIDATION job. A missing file is still registered, so it shows up as FAILED
instead of silently not existing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

from app.db import tenant_tx
from app.validation import sha256_file

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    created: int = 0
    already_present: int = 0


def ingest_manifest(
    conn: psycopg.Connection, manifest_path: str | Path, max_attempts: int = 5
) -> IngestReport:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    names = {t["id"]: t.get("name", t["id"]) for t in manifest.get("tenants", [])}

    by_tenant: dict[str, list[dict]] = {}
    for rec in manifest["recordings"]:
        by_tenant.setdefault(rec["tenant_id"], []).append(rec)

    report = IngestReport()
    for tenant_id, recs in by_tenant.items():
        # Deterministic order (recorded_at, id): it is also the duplicate tie-break order.
        recs.sort(key=lambda r: (r["recorded_at"], r["id"]))
        with tenant_tx(conn, tenant_id):
            conn.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (tenant_id, names.get(tenant_id, tenant_id)),
            )
            for rec in recs:
                path = base / rec["file"]
                try:
                    audio_hash, size = sha256_file(path)
                except OSError:
                    audio_hash, size = None, None
                row = conn.execute(
                    """
                    INSERT INTO recordings
                        (tenant_id, id, file_path, language, channel, recorded_at,
                         audio_hash, audio_bytes, current_step)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'VALIDATION')
                    ON CONFLICT (tenant_id, id) DO NOTHING
                    RETURNING id
                    """,
                    (tenant_id, rec["id"], rec["file"], rec.get("language"),
                     rec.get("channel"), rec["recorded_at"], audio_hash, size),
                ).fetchone()
                if row is None:
                    report.already_present += 1
                    continue
                conn.execute(
                    "INSERT INTO jobs (tenant_id, recording_id, step, max_attempts)"
                    " VALUES (%s, %s, 'VALIDATION', %s)",
                    (tenant_id, rec["id"], max_attempts),
                )
                conn.execute(
                    "INSERT INTO recording_events"
                    " (tenant_id, recording_id, event, to_status, detail)"
                    " VALUES (%s, %s, 'ingested', 'PENDING', %s)",
                    (tenant_id, rec["id"], f"sha256={audio_hash}" if audio_hash else "file missing"),
                )
                report.created += 1
    log.info("ingest: %s new, %s already present", report.created, report.already_present)
    return report
