"""HTTP client for the (unreliable) CRM.

Every call is retried in-process on transient failures with exponential backoff and
full jitter: 429 (honouring Retry-After), 500/502/503/504, timeouts and dropped
connections. A POST is retried with the SAME Idempotency-Key and the SAME body, always:
the caller passes both in and this client never changes them. If retries run out the
outcome is unknown, and the caller must later resend the same key/body again (it is
persisted before the first send), never pick a new slot.

Final answers returned to the caller: 2xx (created / replayed) and 409 (slot taken).
400/404/422 raise PermanentError: resending cannot change the answer.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.errors import PermanentError, TransientError

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Slot:
    start: str                 # exactly as the CRM sends it, e.g. 2026-11-03T15:00:00+03:00
    end: str
    available: bool

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)


@dataclass(frozen=True)
class CRMResponse:
    status: int
    body: dict
    replayed: bool


class _Throttle:
    """Keep this process under the CRM's 2 requests/second. Other processes are kept
    in check by the 429 + Retry-After handling."""

    def __init__(self, min_interval: float):
        self._min = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, sleep: Callable[[float], None]) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._min
        if delay > 0:
            sleep(delay)


class CRMClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0, max_attempts: int = 6,
                 backoff_base: float = 0.5, backoff_cap: float = 8.0,
                 min_interval: float = 0.55, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url,
                                    timeout=httpx.Timeout(timeout, connect=5.0),
                                    transport=transport)
        self._timeout = timeout
        self.max_attempts = max_attempts
        self._base, self._cap = backoff_base, backoff_cap
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._throttle = _Throttle(min_interval)
        self.sent = 0   # requests actually sent (for tests / logs)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ public

    def get_slots(self) -> list[Slot]:
        body = self._request("GET", "/slots").body
        return [Slot(s["start"], s["end"], bool(s["available"])) for s in body["slots"]]

    def list_bookings(self) -> list[dict]:
        return self._request("GET", "/bookings").body["bookings"]

    def list_complaints(self) -> list[dict]:
        return self._request("GET", "/complaints").body["complaints"]

    def create_booking(self, body: dict, idempotency_key: str) -> CRMResponse:
        return self._request("POST", "/bookings", json=body, key=idempotency_key)

    def create_complaint(self, body: dict, idempotency_key: str) -> CRMResponse:
        return self._request("POST", "/complaints", json=body, key=idempotency_key)

    # ---------------------------------------------------------------- internal

    def _backoff(self, attempt: int) -> float:
        # Full jitter (AWS Architecture Blog, "Exponential Backoff And Jitter").
        return self._rng.uniform(0, min(self._cap, self._base * 2 ** attempt))

    def _request(self, method: str, path: str, *, json: dict | None = None,
                 key: str | None = None) -> CRMResponse:
        headers = {"Idempotency-Key": key} if key else {}
        last = "no attempt made"
        delay = 0.0
        for attempt in range(self.max_attempts):
            if attempt:
                self._sleep(delay)          # backoff decided by the previous attempt
            self._throttle.wait(self._sleep)
            delay = self._backoff(attempt)
            try:
                self.sent += 1
                resp = self._client.request(method, path, json=json, headers=headers)
            except httpx.TimeoutException:
                last = f"{method} {path}: no answer within {self._timeout:.0f}s"
                continue
            except httpx.TransportError as exc:
                # Includes "server closed the connection without a response": the write
                # may have been committed. Same key + same body next time.
                last = f"{method} {path}: {type(exc).__name__}: {exc}"
                continue

            if resp.status_code in TRANSIENT_STATUS:
                last = f"{method} {path}: HTTP {resp.status_code}"
                if resp.status_code == 429:
                    try:
                        delay = max(delay, float(resp.headers.get("retry-after", "1")))
                    except ValueError:
                        delay = max(delay, 1.0)
                continue

            try:
                body = resp.json()
            except ValueError:
                last = f"{method} {path}: HTTP {resp.status_code} with a non-JSON body"
                continue
            if resp.status_code < 300 or resp.status_code == 409:
                return CRMResponse(resp.status_code, body,
                                   resp.headers.get("idempotent-replayed") == "true")
            raise PermanentError("crm_rejected",
                                 f"{method} {path}: HTTP {resp.status_code} {body}"[:500])

        raise TransientError("crm_unavailable",
                             f"{last} (after {self.max_attempts} attempts)")
