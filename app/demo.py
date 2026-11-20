"""`demo-chaos`: one command that shows the guarantees under failure.

  0. uses its own database (<PGDATABASE>_demo), so running workers of the real dataset
     never see demo jobs and demo data never mixes with real data
  1. starts a private crm-sim with CHAOS=1 (so the real dataset's slots are untouched)
  2. ingests a small synthetic call set for tenant `demo`: meetings, complaints, a
     byte-identical duplicate and a truncated file
  3. runs several worker processes, SIGKILLs one in the middle of a job, starts a
     replacement
  4. waits until everything is terminal, then checks against the CRM itself:
     nothing lost, nothing duplicated

Speech-to-text is SCRIPTED here (DemoSTT), so the demo needs no API key and is
repeatable. Everything else (queue, leases, masking, encryption, CRM client, booking,
idempotency) is the production code path.
"""

from __future__ import annotations

import array
import io
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from collections import Counter
from dataclasses import replace
from pathlib import Path

import httpx
import psycopg

from app.db import connect, tenant_tx
from app.ingest import ingest_manifest
from app.steps import build_handlers
from app.stt import ChannelTranscript, Segment

REPO = Path(__file__).resolve().parent.parent
TENANT = "demo"

AGENT_LINE = ("Harbor müşteri hizmetleri, ben Zeynep, size nasıl yardımcı olabilirim?", 0.0, 2.5)
SCRIPTS = {
    "meet_complain": [
        ("Merhaba, adım Ayşe Yılmaz, numaram 0532 123 45 67.", 3.0, 5.5),
        ("Faturam iki kez çekildi, iade istiyorum.", 6.0, 8.0),
        ("Bir de ekibinizle toplantı yapmak istiyorum, pazartesi saat 15'te olur mu?", 8.5, 11.0),
    ],
    "meet": [("Sizinle bir görüşme ayarlamak istiyorum, perşembe öğleden sonra.", 3.0, 6.0)],
    "complaint": [("Kargom iki haftadır gecikti, mehmet.kaya@example.com adresime dönün.",
                   3.0, 6.0)],
    "nothing": [("Sadece çalışma saatlerinizi sormak istedim, teşekkürler.", 3.0, 5.0)],
}


class DemoSTT:
    """Scripted transcripts, chosen by the call's kind. DEMO_STT_DELAY slows each call
    down so a worker can be killed mid-job."""

    name, model = "demo-scripted", "scripted-v1"

    def transcribe(self, wav, filename, language):
        time.sleep(float(os.environ.get("DEMO_STT_DELAY", "0")))
        kind = filename.split("__", 1)[1].rsplit("_ch", 1)[0].rstrip("0123456789").rstrip("_")
        lines = [AGENT_LINE] if filename.endswith("_ch0.wav") else SCRIPTS[kind]
        segs = [Segment(a, b, t) for t, a, b in lines]
        return ChannelTranscript(segs, " ".join(s.text for s in segs), "tr", 12.0)


def demo_handlers():
    return build_handlers(stt=DemoSTT())


# ---------------------------------------------------------------------- helpers

def _tone(seconds: float, freq: float, rate: int = 8000) -> bytes:
    n = int(seconds * rate)
    samples = array.array("h", (int(3000 * math.sin(2 * math.pi * freq * i / rate))
                                for i in range(n) for _ in (0, 1)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_dataset(root: Path, run: str) -> tuple[Path, dict[str, str]]:
    """Synthetic calls. Returns (manifest, expected terminal status per recording)."""
    audio = root / "audio"
    audio.mkdir(parents=True)
    kinds = ["meet_complain", "meet_complain", "meet", "meet", "complaint", "complaint",
             "nothing"]
    recs, expected = [], {}
    for i, kind in enumerate(kinds):
        rid = f"{run}__{kind}{i}"
        (audio / f"{rid}.wav").write_bytes(_tone(2.0, 220 + 55 * i))
        recs.append({"id": rid, "file": f"audio/{rid}.wav", "minute": i})
        expected[rid] = "COMPLETED"
    first = recs[0]["id"]
    dup = f"{run}__meet_complain_dup"
    (audio / f"{dup}.wav").write_bytes((audio / f"{first}.wav").read_bytes())
    recs.append({"id": dup, "file": f"audio/{dup}.wav", "minute": 30})
    expected[dup] = "DUPLICATE"
    bad = f"{run}__nothing_truncated"
    (audio / f"{bad}.wav").write_bytes(_tone(10.0, 900)[:2048])
    recs.append({"id": bad, "file": f"audio/{bad}.wav", "minute": 31})
    expected[bad] = "FAILED"

    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "tenants": [{"id": TENANT, "name": "Demo tenant (synthetic calls)"}],
        "recordings": [{"id": r["id"], "tenant_id": TENANT, "file": r["file"], "language": "tr",
                        "channel": "phone",
                        "recorded_at": f"2026-10-30T09:{r['minute']:02d}:00+03:00"}
                       for r in recs],
    }))
    return manifest, expected


def _statuses(conn, run: str) -> dict[str, dict]:
    with tenant_tx(conn, TENANT):
        return {r["id"]: r for r in conn.execute(
            "SELECT id, status::text, current_step::text, failed_step::text, retry_count,"
            " error_code FROM recordings WHERE id LIKE %s ORDER BY id", (f"{run}__%",))}


# -------------------------------------------------------------------------- run

def run_demo(settings, console, workers: int = 3, seed: str | None = None,
             timeout: float = 300.0) -> int:
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table

    from app.migrate import migrate

    run = "d" + uuid.uuid4().hex[:6]
    seed = seed or str(int(time.time()))
    settings = replace(settings, database=f"{settings.database}_demo")
    # Every run starts from an empty demo database. The audio is the same each time, so a
    # leftover database would classify the whole run as duplicates of the previous one.
    with psycopg.connect(settings.admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{settings.database}" WITH (FORCE)')
    migrate(settings)
    tmp = Path(tempfile.mkdtemp(prefix="callvault-demo-"))
    manifest, expected = build_dataset(tmp, run)

    console.rule(f"[bold]Callvault demo-chaos[/]  run={run}  CRM CHAOS=1 seed={seed}  "
                 f"database={settings.database}")
    port = _free_port()
    crm_url = f"http://127.0.0.1:{port}"
    crm_log = open(tmp / "crm.log", "w")
    crm = subprocess.Popen(
        [sys.executable, str(REPO / "crm-sim" / "server.py")],
        env={**os.environ, "PORT": str(port), "CHAOS": "1", "CHAOS_SEED": seed,
             "STATE_FILE": str(tmp / "crm_state.json")},
        stdout=crm_log, stderr=subprocess.STDOUT)
    procs: list[subprocess.Popen] = []
    killed: tuple[int, str] | None = None
    try:
        deadline = time.time() + 15
        while True:
            try:
                if httpx.get(f"{crm_url}/health", timeout=1).json().get("chaos"):
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                console.print("[red]crm-sim did not start[/]")
                return 1
            time.sleep(0.2)
        console.print(f"[green]✓[/] private crm-sim on {crm_url} (CHAOS=1: 429 / 500 / "
                      f"8 s stalls / lost responses)")

        with connect(settings.worker_url) as conn:
            report = ingest_manifest(conn, manifest, max_attempts=10)
        console.print(f"[green]✓[/] ingested {report.created} synthetic calls for tenant "
                      f"[bold]{TENANT}[/] (incl. 1 byte-identical duplicate, 1 truncated file)")

        env = {**os.environ, "PGDATABASE": settings.database, "CRM_BASE_URL": crm_url,
               "LEASE_SECONDS": "4",
               "HEARTBEAT_SECONDS": "1", "POLL_SECONDS": "0.2", "DEMO_STT_DELAY": "1.5",
               "PYTHONPATH": str(REPO)}

        def spawn() -> subprocess.Popen:
            p = subprocess.Popen(
                [sys.executable, "-m", "app.cli", "worker", "--manifest", str(manifest),
                 "--handlers", "app.demo:demo_handlers"],
                cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)
            return p

        for _ in range(workers):
            spawn()
        console.print(f"[green]✓[/] started {workers} worker processes")

        def table(states) -> Table:
            t = Table(title="recordings (tenant demo)", expand=False)
            for col in ("recording", "status", "step", "retries", "detail"):
                t.add_column(col)
            colors = {"COMPLETED": "green", "FAILED": "red", "DUPLICATE": "cyan"}
            for rid, r in states.items():
                c = colors.get(r["status"], "yellow")
                t.add_row(escape(rid.split("__", 1)[1]), f"[{c}]{r['status']}[/]",
                          r["failed_step"] or r["current_step"] or "-", str(r["retry_count"]),
                          r["error_code"] or "")
            return t

        with connect(settings.worker_url) as conn, Live(console=console,
                                                        refresh_per_second=4) as live:
            deadline = time.time() + timeout
            while time.time() < deadline:
                states = _statuses(conn, run)
                live.update(table(states))
                if killed is None:
                    victim = conn.execute(
                        "SELECT recording_id, locked_by FROM jobs WHERE tenant_id = %s"
                        " AND state = 'running' AND step = 'TRANSCRIPTION'"
                        " AND recording_id LIKE %s LIMIT 1", (TENANT, f"{run}__%")).fetchone()
                    if victim:
                        pid = int(victim["locked_by"].rsplit(":", 1)[1])
                        proc = next((p for p in procs if p.pid == pid), None)
                        if proc:
                            proc.kill()             # SIGKILL: no cleanup, lease left behind
                            proc.wait(timeout=10)
                            killed = (pid, victim["recording_id"])
                            live.console.print(
                                f"[bold red]✗ SIGKILL[/] worker pid {pid} in the middle of "
                                f"TRANSCRIPTION of {victim['recording_id'].split('__', 1)[1]}")
                            spawn()
                            live.console.print("[green]✓[/] started a replacement worker")
                if all(r["status"] in ("COMPLETED", "FAILED", "DUPLICATE")
                       for r in states.values()):
                    break
                time.sleep(0.25)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)
        crm.kill()
        crm.wait(timeout=10)
        crm_log.close()

    # ------------------------------------------------------------------ verdict
    with connect(settings.worker_url) as conn:
        states = _statuses(conn, run)
        with tenant_tx(conn, TENANT):
            reclaims = conn.execute(
                "SELECT count(*) AS n FROM recording_events WHERE event = 'lease_reclaimed'"
                " AND recording_id LIKE %s", (f"{run}__%",)).fetchone()["n"]
            retries = conn.execute(
                "SELECT count(*) AS n FROM recording_events WHERE event = 'retry_scheduled'"
                " AND recording_id LIKE %s", (f"{run}__%",)).fetchone()["n"]
    state_file = tmp / "crm_state.json"      # only exists once the CRM stored something
    state = (json.loads(state_file.read_text()) if state_file.exists()
             else {"bookings": [], "complaints": []})
    bookings = [b for b in state["bookings"] if b["call_id"].startswith(run)]
    complaints = [c for c in state["complaints"] if c["call_id"].startswith(run)]
    dup_b = sum(n - 1 for n in Counter(b["call_id"] for b in bookings).values())
    dup_c = sum(n - 1 for n in Counter(c["call_id"] for c in complaints).values())
    want_b = {r for r, s in expected.items() if s == "COMPLETED" and "__meet" in r}
    want_c = {r for r, s in expected.items() if s == "COMPLETED"
              and ("complain" in r.split("__", 1)[1])}
    log = (tmp / "crm.log").read_text()
    faults = {code: log.count(f'" {code} ') for code in (429, 500)}
    lost = [r for r in expected if r not in states
            or states[r]["status"] not in ("COMPLETED", "FAILED", "DUPLICATE")]
    wrong = {r: states[r]["status"] for r in expected
             if r in states and states[r]["status"] != expected[r]}

    checks = [
        (not lost, f"recordings lost / unfinished: {len(lost)}"),
        (not wrong, escape(f"unexpected final statuses: {wrong or 0}")),
        (dup_b == 0 and dup_c == 0, f"duplicate CRM records: bookings {dup_b}, complaints {dup_c}"),
        ({b["call_id"] for b in bookings} == want_b,
         f"bookings in CRM: {len(bookings)} (expected {len(want_b)})"),
        ({c["call_id"] for c in complaints} == want_c,
         f"complaints in CRM: {len(complaints)} (expected {len(want_c)})"),
        (killed is not None and reclaims >= 1,
         f"worker SIGKILLed mid-job: {'pid ' + str(killed[0]) if killed else 'no'}, "
         f"jobs reclaimed after lease expiry: {reclaims}"),
    ]
    lines = [f"[{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] {text}" for ok, text in checks]
    lines.append(f"[dim]CRM chaos absorbed: {faults[429]}×429, {faults[500]}×500, "
                 f"{retries} step retries scheduled; duplicate audio → DUPLICATE, "
                 f"truncated audio → FAILED(VALIDATION)[/]")
    ok = all(c for c, _ in checks)
    console.print()
    console.print(Panel("\n".join(lines), title="[bold]verdict: " +
                        ("[green]0 lost, 0 duplicates[/]" if ok else "[red]FAILED[/]"),
                        expand=False))
    return 0 if ok else 1
