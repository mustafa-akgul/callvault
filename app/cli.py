"""Command line entry point: python -m app.cli <command>

Operator commands (database roles app_owner / app_worker):
  migrate                               create roles/database, apply migrations, create key
  ingest [--manifest PATH]              register recordings from manifest.json (idempotent)
  worker [--exit-when-idle]             process jobs; run several for parallelism
  reprocess (--recording-id ID [--tenant T] | --all-failed [--tenant T])
                                        resume FAILED recordings at the step that failed
  issue-key --tenant T [--label L]      create an API key for a tenant (printed once)
  demo-chaos [--workers N] [--seed S]   live failure demo: CHAOS=1 CRM + a SIGKILLed worker

Tenant commands (app_user role; the tenant comes from the API key, never from a flag):
  list-recordings [--recording-id ID] [--events]
  get-transcript --recording-id ID      masked transcript only
  list-actions                          meetings (asked vs. booked) and complaints
  metrics                               status, retries, PII and CRM statistics
  Authenticate with CALLVAULT_API_KEY=<key> or --api-key. An optional --tenant is checked
  against the key and refused if it does not match.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from app import config
from app.db import connect

TERMINAL = {"COMPLETED": "green", "FAILED": "red", "DUPLICATE": "cyan"}


def _console() -> Console:
    c = Console()
    return c if c.is_terminal else Console(width=160, soft_wrap=True)


def _load_handlers(spec: str | None):
    if not spec:
        return None
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)()


def _fmt_dt(v) -> str:
    """Show times the way callers and the CRM say them: Europe/Istanbul (+03:00),
    whatever the database session's time zone is."""
    from app.timeparse_tr import TR_TZ
    return v.astimezone(TR_TZ).isoformat(timespec="minutes") if v else "-"


# ------------------------------------------------------------------- operators

def cmd_migrate(settings, args, console) -> int:
    from app.crypto import ensure_key_file
    from app.migrate import migrate
    applied = migrate(settings)
    console.print(f"migrations applied: {', '.join(applied) if applied else 'none (up to date)'}")
    if ensure_key_file():
        console.print("created transcript encryption key file (TRANSCRIPT_KEY_FILE)")
    return 0


def cmd_ingest(settings, args, console) -> int:
    from app.ingest import ingest_manifest
    with connect(settings.worker_url) as conn:
        report = ingest_manifest(conn, args.manifest or settings.manifest_path,
                                 max_attempts=settings.max_attempts)
    console.print(f"ingested: {report.created} new, {report.already_present} already present")
    return 0


def cmd_worker(settings, args, console) -> int:
    from app.worker import Worker
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker = Worker(
        settings.worker_url,
        audio_root=Path(args.manifest or settings.manifest_path).resolve().parent,
        handlers=_load_handlers(args.handlers),
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        poll_seconds=settings.poll_seconds,
    )
    try:
        worker.run(stop, exit_when_idle=args.exit_when_idle)
    finally:
        worker.close()
    return 0


def cmd_reprocess(settings, args, console) -> int:
    from app.queue import STEPS, STATUS_FOR_STEP, reprocess
    if not (args.recording_id or args.all_failed):
        console.print("[red]give --recording-id ID or --all-failed[/]")
        return 2
    with connect(settings.worker_url) as conn:
        requeued = reprocess(conn, tenant_id=args.tenant, recording_id=args.recording_id)
    for tenant, rec, step in requeued:
        skipped = ", ".join(STEPS[: STEPS.index(step)]) or "nothing"
        console.print(f"requeued [bold]{escape(tenant)}/{escape(rec)}[/] at {step} "
                      f"(status {STATUS_FOR_STEP[step]}); kept, not re-run: {skipped}")
    console.print(f"{len(requeued)} recording(s) requeued")
    return 0 if requeued or args.all_failed else 1


def cmd_remask(settings, args, console) -> int:
    from app.crypto import TranscriptCipher
    from app.remask import remask
    with connect(settings.worker_url) as conn:
        results = remask(conn, TranscriptCipher.from_env(), tenant_id=args.tenant,
                         recording_id=args.recording_id)
    changed = [r for r in results if r.changed or r.error or r.actions_kept]
    for r in changed:
        kinds = sorted(set(r.before) | set(r.after))
        diff = ", ".join(f"{k} {r.before.get(k, 0)} -> {r.after.get(k, 0)}" for k in kinds
                         if r.before.get(k, 0) != r.after.get(k, 0)) or \
            "text updated, PII counts unchanged"
        line = f"remasked [bold]{escape(r.tenant_id)}/{escape(r.recording_id)}[/]: {diff}"
        if r.actions_scrubbed:
            line += f"; stored CRM copies rewritten: {r.actions_scrubbed}"
        console.print(line)
        if r.sent_to_crm:
            console.print(f"  [yellow]already in the CRM with the old text, correct it there: "
                          f"{escape(', '.join(r.sent_to_crm))}[/]")
        if r.actions_kept:
            console.print(f"  [yellow]{r.actions_kept} action(s) not final, body kept "
                          f"(would be resent with the same Idempotency-Key)[/]")
        if r.error:
            console.print(f"  [red]{escape(r.error)}[/]")
    console.print(f"{len(results)} completed recording(s) checked, {len(changed)} changed")
    return 1 if any(r.error for r in results) else 0


def cmd_issue_key(settings, args, console) -> int:
    from app.tenant_auth import AuthError, issue_key
    with connect(settings.worker_url) as conn:
        try:
            key = issue_key(conn, args.tenant, args.label)
        except AuthError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 2
    console.print(f"API key for tenant [bold]{args.tenant}[/] (shown once, store it safely):")
    print(key)
    return 0


def cmd_demo_chaos(settings, args, console) -> int:
    from app.demo import run_demo
    return run_demo(settings, console, workers=args.workers, seed=args.seed)


# ---------------------------------------------------------------------- tenant

def _tenant(conn, args, console) -> str | None:
    from app.tenant_auth import AuthError, authenticate
    try:
        return authenticate(conn, args.api_key or os.environ.get("CALLVAULT_API_KEY"),
                            claimed_tenant=args.tenant)
    except AuthError as exc:
        console.print(f"[red]authentication failed:[/] {escape(str(exc))}")
        return None


def cmd_list_recordings(settings, args, console) -> int:
    from app.tenant_api import list_recordings, recording_events
    with connect(settings.user_url) as conn:
        tenant = _tenant(conn, args, console)
        if tenant is None:
            return 2
        rows = list_recordings(conn, tenant, args.recording_id)
        if not rows:
            console.print("no recordings" if not args.recording_id else "recording not found")
            return 0 if not args.recording_id else 1
        t = Table(title=f"recordings of tenant {tenant}")
        for col in ("recording", "status", "step", "retries", "detail"):
            t.add_column(col, overflow="fold")
        for r in rows:
            c = TERMINAL.get(r["status"], "yellow")
            detail = (f"duplicate of {r['duplicate_of']}" if r["duplicate_of"] else
                      f"{r['error_code']}: {r['error_message']}" if r["error_code"] else "")
            t.add_row(escape(r["id"]), f"[{c}]{r['status']}[/]",
                      r["failed_step"] or r["current_step"] or "-", str(r["retry_count"]),
                      escape(detail))
        console.print(t)
        if args.events and args.recording_id:
            e = Table(title=f"history of {args.recording_id}")
            for col in ("time", "event", "step", "from", "to", "detail"):
                e.add_column(col, overflow="fold")
            for ev in recording_events(conn, tenant, args.recording_id):
                e.add_row(f"{ev['at']:%H:%M:%S}", ev["event"], ev["step"] or "",
                          ev["from_status"] or "", ev["to_status"] or "",
                          escape(ev["detail"] or ""))
            console.print(e)
    return 0


def cmd_get_transcript(settings, args, console) -> int:
    from app.tenant_api import get_transcript
    with connect(settings.user_url) as conn:
        tenant = _tenant(conn, args, console)
        if tenant is None:
            return 2
        row = get_transcript(conn, tenant, args.recording_id)
    if row is None:
        console.print("recording not found")
        return 1
    if not row["masked_text"]:
        console.print(f"no masked transcript yet (status {row['status']})")
        return 1
    console.print(f"[bold]{escape(row['id'])}[/]  status={row['status']}  "
                  + escape(f"stt={row['provider']}/{row['model']}  "
                           f"roles={row['stt_meta'].get('roles')}  masked={row['pii_summary']}"))
    console.print(row["masked_text"], markup=False, highlight=False)
    return 0


def cmd_list_actions(settings, args, console) -> int:
    from app.tenant_api import list_actions
    with connect(settings.user_url) as conn:
        tenant = _tenant(conn, args, console)
        if tenant is None:
            return 2
        rows = list_actions(conn, tenant)
    if not rows:
        console.print("no meetings or complaints")
        return 0
    t = Table(title=f"meetings and complaints of tenant {tenant}")
    for col in ("recording", "action", "status", "asked", "booked / summary", "CRM id"):
        t.add_column(col, overflow="fold")
    for r in rows:
        c = {"SUCCEEDED": "green", "NEEDS_FOLLOWUP": "yellow", "FAILED": "red"}.get(
            r["status"], "white")
        if r["action_type"] == "booking":
            asked = _fmt_dt(r["requested_start"]) if r["requested_start"] else \
                "flexible: " + ", ".join(f"{k}={v}" for k, v in (r["requested_window"] or {}).items()
                                         if v and k in ("date_from", "date_to", "weekdays",
                                                        "hour_from", "hour_to"))
            out = _fmt_dt(r["booked_slot_start"]) if r["booked_slot_start"] else \
                (r["followup_reason"] or r["last_error"] or "")
        else:
            asked, out = "-", (r["request_payload"] or {}).get("summary", "")
        t.add_row(escape(r["recording_id"]), r["action_type"], f"[{c}]{r['status']}[/]",
                  escape(asked), escape(out), escape(r["external_id"] or "-"))
    console.print(t)
    return 0


def cmd_metrics(settings, args, console) -> int:
    from app.tenant_api import metrics
    with connect(settings.user_url) as conn:
        tenant = _tenant(conn, args, console)
        if tenant is None:
            return 2
        m = metrics(conn, tenant)

    t = Table(title=f"metrics of tenant {tenant}", show_header=True)
    t.add_column("area", style="bold")
    t.add_column("metric")
    t.add_column("value", justify="right")
    for status, n in sorted(m["by_status"].items()):
        t.add_row("recordings", f"[{TERMINAL.get(status, 'yellow')}]{status}[/]", str(n))
    t.add_row("recordings", "total", str(m["total"]))
    for step, n in sorted(m["failed_by_step"].items()):
        t.add_row("failures", f"failed at {step}", f"[red]{n}[/]")
    r = m["retries"]
    t.add_row("reliability", "retries (total / recordings / max)",
              f"{r['total']} / {r['recordings']} / {r['max']}")
    t.add_row("reliability", "retries scheduled", str(m["events"].get("retry_scheduled", 0)))
    t.add_row("reliability", "leases reclaimed (killed workers)",
              str(m["events"].get("lease_reclaimed", 0)))
    t.add_row("reliability", "reprocess requests", str(m["events"].get("reprocess_requested", 0)))
    for kind, n in sorted(m["pii"].items()):
        t.add_row("PII masked", kind, str(n))
    for (atype, status), n in sorted(m["actions"].items()):
        t.add_row("CRM", f"{atype} {status}", str(n))
    a = m["booking_attempts"]
    t.add_row("CRM", "booking attempts / sends / 409s / adopted",
              f"{a['attempts']} / {a['sends']} / {a['conflicts']} / {a['adopted']}")
    for rule, n in sorted(m["booking_rules"].items(), key=lambda kv: str(kv[0])):
        t.add_row("CRM", f"booked by rule {rule}", str(n))
    console.print(t)
    return 0


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.cli",
                                description="Callvault call-recording pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create roles/database and apply migrations")
    s = sub.add_parser("ingest", help="register recordings from manifest.json")
    s.add_argument("--manifest")
    s = sub.add_parser("worker", help="process jobs")
    s.add_argument("--manifest", help="audio paths are relative to the manifest's folder")
    s.add_argument("--exit-when-idle", action="store_true")
    s.add_argument("--handlers", help="module:factory returning {step: handler} (tests/demo)")
    s = sub.add_parser("reprocess", help="resume FAILED recordings at their failed step")
    s.add_argument("--recording-id")
    s.add_argument("--tenant")
    s.add_argument("--all-failed", action="store_true")
    s = sub.add_parser("remask", help="re-apply current masking rules to COMPLETED recordings")
    s.add_argument("--recording-id")
    s.add_argument("--tenant")
    s = sub.add_parser("issue-key", help="create an API key for a tenant")
    s.add_argument("--tenant", required=True)
    s.add_argument("--label")
    s = sub.add_parser("demo-chaos", help="live failure demo")
    s.add_argument("--workers", type=int, default=3)
    s.add_argument("--seed")

    for name, extra in (("list-recordings", ("recording-id", "events")),
                        ("get-transcript", ("recording-id!",)),
                        ("list-actions", ()), ("metrics", ())):
        s = sub.add_parser(name)
        s.add_argument("--api-key", help="defaults to $CALLVAULT_API_KEY")
        s.add_argument("--tenant", help="optional; must match the key's tenant")
        for opt in extra:
            if opt == "events":
                s.add_argument("--events", action="store_true")
            else:
                s.add_argument("--recording-id", required=opt.endswith("!"))
    args = p.parse_args(argv)

    if args.command in ("list-recordings", "get-transcript", "list-actions", "metrics",
                        "demo-chaos"):
        logging.getLogger().setLevel(logging.WARNING)
    settings = config.load()
    console = _console()
    handler = {
        "migrate": cmd_migrate, "ingest": cmd_ingest, "worker": cmd_worker,
        "reprocess": cmd_reprocess, "remask": cmd_remask, "issue-key": cmd_issue_key,
        "demo-chaos": cmd_demo_chaos,
        "list-recordings": cmd_list_recordings, "get-transcript": cmd_get_transcript,
        "list-actions": cmd_list_actions, "metrics": cmd_metrics,
    }[args.command]
    return handler(settings, args, console)


if __name__ == "__main__":
    sys.exit(main())
