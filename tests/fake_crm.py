"""In-memory CRM with the same contract as crm-sim/server.py, plus scripted faults.

Use with httpx.MockTransport. Faults are consumed in order per (method, path):
  "500" "503" "429"   error response before anything is written
  "timeout"           httpx.ReadTimeout before anything is written
  "disconnect"        connection dropped before anything is written
  "drop_after_commit" the write is committed, then the connection drops (CHAOS lost
                      response): the client never learns it succeeded
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from pathlib import Path

import httpx

CALENDAR = json.loads((Path(__file__).resolve().parent.parent / "crm-sim" / "calendar.json")
                      .read_text())


class FakeCRM:
    def __init__(self):
        self.slots = [dict(s) for s in CALENDAR["slots"]]
        self.prebooked = {s["start"] for s in self.slots if not s["available"]}
        self.bookings: list[dict] = []
        self.complaints: list[dict] = []
        self.idem: dict[str, dict] = {}
        self.faults: dict[tuple[str, str], deque] = defaultdict(deque)
        self.requests: list[tuple[str, str, str | None, dict | None]] = []
        self.before_post_booking = None    # hook(body) -> e.g. steal the slot
        self._lock = threading.Lock()
        self._n = 0

    # ------------------------------------------------------------- scripting
    def fail(self, method: str, path: str, *modes: str) -> None:
        self.faults[(method, path)].extend(modes)

    def take(self, slot_start: str) -> None:
        """Someone else books this slot."""
        self.prebooked.add(slot_start)

    def posts(self, path: str) -> list:
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    # -------------------------------------------------------------- behaviour
    def _taken(self) -> set[str]:
        return self.prebooked | {b["slot_start"] for b in self.bookings}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        key = request.headers.get("idempotency-key")
        with self._lock:
            self.requests.append((method, path, key, body))
            q = self.faults[(method, path)]
            mode = q.popleft() if q else None
        if mode in ("500", "503"):
            return httpx.Response(int(mode), json={"error": "internal_error"})
        if mode == "429":
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate_limited"})
        if mode == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if mode == "disconnect":
            raise httpx.RemoteProtocolError("Server disconnected", request=request)

        with self._lock:
            if method == "GET" and path == "/slots":
                taken = self._taken()
                return httpx.Response(200, json={"timezone": "Europe/Istanbul", "slots": [
                    {**s, "available": s["start"] not in taken} for s in self.slots]})
            if method == "GET" and path == "/bookings":
                return httpx.Response(200, json={"bookings": list(self.bookings)})
            if method == "GET" and path == "/complaints":
                return httpx.Response(200, json={"complaints": list(self.complaints)})
            if method != "POST" or path not in ("/bookings", "/complaints"):
                return httpx.Response(404, json={"error": "not_found"})
            if not key:
                return httpx.Response(400, json={"error": "missing_idempotency_key"})
            seen = self.idem.get(key)
            if seen:
                if seen["path"] != path or seen["request"] != body:
                    return httpx.Response(422, json={
                        "error": "idempotency_key_reused_with_different_request"})
                return httpx.Response(seen["status"], json=seen["response"],
                                      headers={"Idempotent-Replayed": "true"})
            if path == "/bookings":
                if self.before_post_booking:
                    self.before_post_booking(body)
                status, resp = self._book(body)
            else:
                status, resp = self._complain(body)
            if status in (200, 201, 409):
                self.idem[key] = {"path": path, "request": body, "status": status,
                                  "response": resp}
        if mode == "drop_after_commit" and status == 201:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request)
        return httpx.Response(status, json=resp)

    def _book(self, body):
        slot = body.get("slot_start")
        if not body.get("call_id") or not slot:
            return 422, {"error": "call_id_and_slot_start_required"}
        if slot not in {s["start"] for s in self.slots}:
            return 422, {"error": "unknown_slot"}
        if slot in self._taken():
            return 409, {"error": "slot_taken"}
        self._n += 1
        b = {"id": f"bk_{self._n:04d}", "call_id": body["call_id"], "slot_start": slot,
             "requested_start": body.get("requested_start"), "note": body.get("note")}
        self.bookings.append(b)
        return 201, b

    def _complain(self, body):
        if not body.get("call_id") or not body.get("summary"):
            return 422, {"error": "call_id_and_summary_required"}
        self._n += 1
        c = {"id": f"cp_{self._n:04d}", "call_id": body["call_id"], "summary": body["summary"]}
        self.complaints.append(c)
        return 201, c
