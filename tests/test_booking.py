"""Slot selection against the real CRM calendar (crm-sim/calendar.json).

Pre-booked in the calendar: 10-30 13:00, 11-02 12:00 + 13:00, 11-03 16:00 + 17:00,
11-05 10:00. Slots start 10:00..17:00 on weekdays.
"""

from datetime import date, datetime

import pytest

from app.booking import choose_slot
from app.crm import Slot
from app.timeparse_tr import TR_TZ, TimeRequest, parse_time_request
from tests.fake_crm import CALENDAR

CALL = datetime(2026, 10, 30, 9, 0, tzinfo=TR_TZ)


def slots(taken=()):
    return [Slot(s["start"], s["end"], s["available"] and s["start"] not in taken)
            for s in CALENDAR["slots"]]


def pick(text, taken=(), call=CALL, exclude=frozenset()):
    slot, rule = choose_slot(parse_time_request(text, call), slots(taken), call, exclude)
    return (slot.start if slot else None), rule


def test_exact_slot_free():
    assert pick("pazartesi saat 15'te") == ("2026-11-02T15:00:00+03:00", "exact")


def test_exact_slot_taken_books_next_after():
    # 11-03 16:00 and 17:00 are pre-booked -> next free is 11-04 10:00.
    assert pick("3 Kasım saat 16'da") == ("2026-11-04T10:00:00+03:00", "next_after_requested")


def test_exact_time_between_slots_books_next_after():
    assert pick("3 Kasım saat 14:30") == ("2026-11-03T15:00:00+03:00", "next_after_requested")


def test_exact_time_on_weekend_rolls_to_monday():
    assert pick("yarın saat 2'de") == ("2026-11-02T10:00:00+03:00", "next_after_requested")


def test_flexible_weekday_afternoon():
    assert pick("perşembe öğleden sonra") == ("2026-11-05T13:00:00+03:00", "first_matching")


def test_flexible_weekday_rolls_to_next_week_when_full():
    taken = [f"2026-11-05T{h}:00:00+03:00" for h in range(13, 18)]
    assert pick("perşembe öğleden sonra", taken) == \
        ("2026-11-12T13:00:00+03:00", "first_matching")


def test_flexible_fixed_day_full_needs_followup():
    taken = [f"2026-11-03T{h}:00:00+03:00" for h in range(10, 16)]   # 16, 17 pre-booked
    slot, reason = pick("3 Kasım'da", taken)
    assert slot is None and "no free slot" in reason


def test_nothing_said_takes_earliest_after_the_call():
    assert pick("en kısa sürede") == ("2026-10-30T10:00:00+03:00", "first_matching")


def test_never_books_before_the_call():
    late = datetime(2026, 10, 30, 12, 30, tzinfo=TR_TZ)
    # 13:00 is pre-booked -> 14:00, never 10:00-12:00 of the same day.
    assert pick("bugün", call=late) == ("2026-10-30T14:00:00+03:00", "first_matching")


def test_beyond_the_calendar_needs_followup():
    slot, reason = pick("20 Kasım saat 10")
    assert slot is None and "after the requested time" in reason


def test_slots_lost_with_409_are_excluded():
    assert pick("pazartesi saat 15'te", exclude={"2026-11-02T15:00:00+03:00"}) == \
        ("2026-11-02T16:00:00+03:00", "next_after_requested")


def test_time_only_any_day():
    taken = ["2026-10-30T16:00:00+03:00"]
    assert pick("saat 4'te", taken) == ("2026-11-02T16:00:00+03:00", "first_matching")


@pytest.mark.parametrize("req", [TimeRequest(text="", date_from=date(2026, 11, 14))])
def test_empty_calendar_window(req):
    assert choose_slot(req, slots(), CALL)[0] is None
