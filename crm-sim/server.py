"""A deliberately unreliable CRM simulator (stdlib only), used as the pipeline's
external dependency in the chaos demo and in the integration tests.

Endpoints: GET /slots, POST /bookings, POST /complaints, GET /bookings, GET /complaints,
GET /health. Every POST needs an Idempotency-Key header; the same key with the same body
replays the original response, the same key with another body is a 422.

CHAOS=1 turns on the failures a real third-party API produces:
  * 429 above 2 requests per second (with Retry-After)
  * 500 on a share of requests
  * 8 s stalls on a share of requests
  * a write that commits, then the connection closes before the response is sent

Environment: PORT (8080), CHAOS (1), CHAOS_SEED (unset = random), STATE_FILE.
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CALENDAR = json.loads((HERE / "calendar.json").read_text(encoding="utf-8"))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
CHAOS = os.environ.get("CHAOS", "1") == "1"
SEED = os.environ.get("CHAOS_SEED") or None
RNG = random.Random(int(SEED) if SEED else None)

RATE_LIMIT_PER_SECOND = 2
P_500, P_STALL, P_LOST = 0.08, 0.05, 0.06
STALL_SECONDS = 8

LOCK = threading.Lock()
RECENT: deque[float] = deque()
STATE = {"bookings": [], "complaints": [], "idempotency": {}, "counter": 0}


def load_state() -> None:
    if STATE_FILE.exists():
        STATE.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))


def save_state() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE), encoding="utf-8")
    tmp.replace(STATE_FILE)


def taken_slots() -> set[str]:
    prebooked = {s["start"] for s in CALENDAR["slots"] if not s["available"]}
    return prebooked | {b["slot_start"] for b in STATE["bookings"]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Access log on stdout: the chaos tests count the status codes in it.
        status = args[1] if len(args) > 1 else ""
        print(f'{self.address_string()} - "{self.requestline}" {status} -', flush=True)

    # --------------------------------------------------------------- plumbing
    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _drop_connection(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _chaos_gate(self, is_write: bool) -> str | None:
        """None = proceed; "handled" = a failure response was sent; "lost_response" =
        commit the write, then drop the connection instead of answering."""
        if not CHAOS or self.path == "/health":
            return None
        now = time.monotonic()
        with LOCK:
            while RECENT and now - RECENT[0] > 1.0:
                RECENT.popleft()
            RECENT.append(now)
            over_limit = len(RECENT) > RATE_LIMIT_PER_SECOND
            roll = RNG.random()
        if over_limit:
            self._send(429, {"error": "rate_limited"}, {"Retry-After": "1"})
            return "handled"
        if roll < P_500:
            self._send(500, {"error": "internal_error"})
            return "handled"
        if roll < P_500 + P_STALL:
            time.sleep(STALL_SECONDS)
        if is_write and roll > 1 - P_LOST:
            return "lost_response"
        return None

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        if self.path not in ("/bookings", "/complaints") and self._chaos_gate(False) == "handled":
            return
        if self.path == "/health":
            return self._send(200, {"status": "ok", "chaos": CHAOS})
        with LOCK:
            if self.path == "/slots":
                taken = taken_slots()
                slots = [{**s, "available": s["start"] not in taken} for s in CALENDAR["slots"]]
                return self._send(200, {"timezone": CALENDAR["timezone"], "slots": slots})
            if self.path == "/bookings":
                return self._send(200, {"bookings": list(STATE["bookings"])})
            if self.path == "/complaints":
                return self._send(200, {"complaints": list(STATE["complaints"])})
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fault = self._chaos_gate(True)
        if fault == "handled":
            return
        if self.path not in ("/bookings", "/complaints"):
            return self._send(404, {"error": "not_found"})
        key = self.headers.get("Idempotency-Key")
        if not key:
            return self._send(400, {"error": "missing_idempotency_key"})
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(422, {"error": "invalid_json"})

        with LOCK:
            seen = STATE["idempotency"].get(key)
            if seen:
                if seen["path"] != self.path or seen["request"] != body:
                    return self._send(422, {"error": "idempotency_key_reused_with_different_request"})
                return self._send(seen["status"], seen["response"], {"Idempotent-Replayed": "true"})
            status, response = self._book(body) if self.path == "/bookings" else self._complain(body)
            if status in (201, 409):
                STATE["idempotency"][key] = {"path": self.path, "request": body,
                                             "status": status, "response": response}
            save_state()
        if fault == "lost_response" and status == 201:
            self.log_request(201)
            return self._drop_connection()
        self._send(status, response)

    # ---------------------------------------------------------------- domain
    def _next_id(self, prefix: str) -> str:
        STATE["counter"] += 1
        return f"{prefix}_{STATE['counter']:04d}"

    def _book(self, body: dict):
        slot = body.get("slot_start")
        if not body.get("call_id") or not slot:
            return 422, {"error": "call_id_and_slot_start_required"}
        if slot not in {s["start"] for s in CALENDAR["slots"]}:
            return 422, {"error": "unknown_slot"}
        if slot in taken_slots():
            return 409, {"error": "slot_taken"}
        booking = {"id": self._next_id("bk"), "call_id": body["call_id"], "slot_start": slot,
                   "requested_start": body.get("requested_start"), "note": body.get("note")}
        STATE["bookings"].append(booking)
        return 201, booking

    def _complain(self, body: dict):
        if not body.get("call_id") or not body.get("summary"):
            return 422, {"error": "call_id_and_summary_required"}
        complaint = {"id": self._next_id("cp"), "call_id": body["call_id"],
                     "summary": body["summary"]}
        STATE["complaints"].append(complaint)
        return 201, complaint


def main() -> None:
    load_state()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"crm-sim listening on :{port} (chaos={'on' if CHAOS else 'off'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
