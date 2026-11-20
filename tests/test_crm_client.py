"""CRM client: retries with backoff + jitter, same key/body on every retry."""

from __future__ import annotations

import random

import pytest

from app.crm import CRMClient
from app.errors import PermanentError, TransientError
from tests.fake_crm import FakeCRM

BODY = {"call_id": "rec_004", "slot_start": "2026-11-03T15:00:00+03:00",
        "requested_start": "2026-11-03T15:00:00+03:00", "note": "tenant=harbor"}
KEY = "harbor:booking:rec_004:2026-11-03T15:00:00+03:00"


@pytest.fixture
def crm():
    return FakeCRM()


def client(crm, **kw):
    sleeps: list[float] = []
    kw.setdefault("max_attempts", 6)
    c = CRMClient("http://crm", transport=crm.transport(), sleep=sleeps.append,
                  min_interval=0, rng=random.Random(1), **kw)
    return c, sleeps


@pytest.mark.parametrize("faults", [
    ("500",), ("503",), ("429",), ("timeout",), ("disconnect",),
    ("500", "429", "timeout", "disconnect", "503"),
])
def test_transient_failures_are_retried_with_same_key_and_body(crm, faults):
    crm.fail("POST", "/bookings", *faults)
    c, _ = client(crm)
    resp = c.create_booking(BODY, KEY)
    assert resp.status == 201
    sent = crm.posts("/bookings")
    assert len(sent) == len(faults) + 1
    assert {r[2] for r in sent} == {KEY}                    # same key every time
    assert all(r[3] == BODY for r in sent)                  # same body every time
    assert len(crm.bookings) == 1


def test_lost_response_after_commit_is_replayed_not_rebooked(crm):
    crm.fail("POST", "/bookings", "drop_after_commit")
    c, _ = client(crm)
    resp = c.create_booking(BODY, KEY)
    assert resp.status == 201 and resp.replayed
    assert len(crm.bookings) == 1


def test_gives_up_with_a_transient_error(crm):
    crm.fail("POST", "/bookings", *["503"] * 10)
    c, _ = client(crm, max_attempts=4)
    with pytest.raises(TransientError) as exc:
        c.create_booking(BODY, KEY)
    assert exc.value.code == "crm_unavailable"
    assert "HTTP 503" in exc.value.message and "after 4 attempts" in exc.value.message
    assert len(crm.posts("/bookings")) == 4 and crm.bookings == []


def test_backoff_is_exponential_with_full_jitter(crm):
    crm.fail("GET", "/slots", *["500"] * 5)
    c, sleeps = client(crm, backoff_base=0.5, backoff_cap=4.0)
    c.get_slots()
    assert len(sleeps) == 5
    caps = [min(4.0, 0.5 * 2 ** i) for i in range(5)]
    assert all(0 <= s <= cap for s, cap in zip(sleeps, caps))
    assert len(set(sleeps)) > 1   # jittered, not a fixed schedule


def test_retry_after_is_honoured(crm):
    import httpx

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"bookings": []})

    sleeps = []
    c = CRMClient("http://crm", transport=httpx.MockTransport(handler), sleep=sleeps.append,
                  min_interval=0)
    c.list_bookings()
    assert sleeps and sleeps[0] >= 3


def test_409_is_returned_not_retried(crm):
    crm.take(BODY["slot_start"])
    c, _ = client(crm)
    assert c.create_booking(BODY, KEY).status == 409
    assert len(crm.posts("/bookings")) == 1


def test_same_key_different_body_is_permanent(crm):
    c, _ = client(crm)
    c.create_booking(BODY, KEY)
    with pytest.raises(PermanentError) as exc:
        c.create_booking({**BODY, "slot_start": "2026-11-03T14:00:00+03:00"}, KEY)
    assert "422" in exc.value.message


def test_complaint_roundtrip(crm):
    c, _ = client(crm)
    r = c.create_complaint({"call_id": "rec_003", "summary": "Fatura iki kez çekildi"},
                           "harbor:complaint:rec_003")
    assert r.status == 201 and c.list_complaints()[0]["call_id"] == "rec_003"


def test_throttle_spaces_requests():
    import httpx

    sleeps = []
    c = CRMClient("http://crm", sleep=sleeps.append, min_interval=0.5,
                  transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                      "bookings": []})))
    for _ in range(3):
        c.list_bookings()
    # Three back-to-back calls: the 2nd waits ~0.5s, the 3rd ~1.0s (>= 2 req/s never).
    assert len(sleeps) == 2 and 0.4 <= sleeps[0] <= 0.5 and 0.9 <= sleeps[1] <= 1.0
