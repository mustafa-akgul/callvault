"""Time expressions, resolved against a call on Friday 2026-10-30 09:00 (+03:00)."""

from datetime import date, datetime

import pytest

from app.timeparse_tr import TR_TZ, parse_time_request

CALL = datetime(2026, 10, 30, 9, 0, tzinfo=TR_TZ)   # a Friday


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TR_TZ)


@pytest.mark.parametrize("text, exact", [
    ("pazartesi saat 15'te uygun musunuz", at(2026, 11, 2, 15)),
    ("pazartesiye saat 3'te", at(2026, 11, 2, 15)),          # 3 -> 15:00 (office hours)
    ("haftaya salı saat 10", at(2026, 11, 3, 10)),
    ("3 Kasım saat 14:30", at(2026, 11, 3, 14, 30)),
    ("Kasım'ın 4'ü saat 11", at(2026, 11, 4, 11)),
    ("yarın saat 2'de", at(2026, 10, 31, 14)),
    ("salı olmaz, perşembe olur, saat 2'de", at(2026, 11, 5, 14)),   # last mention wins
    ("öğleden sonra 4'te, çarşamba", at(2026, 11, 4, 16)),
    ("cuma saat 11'de", at(2026, 11, 6, 11)),               # said on a Friday: next Friday
    ("cumartesi saat 10", at(2026, 10, 31, 10)),             # not "cuma"
    ("bu hafta cuma saat 4 buçukta", at(2026, 10, 30, 16, 30)),
])
def test_exact(text, exact):
    assert parse_time_request(text, CALL).exact == exact


def test_weekday_and_part_of_day_is_flexible():
    r = parse_time_request("perşembe öğleden sonra görüşelim", CALL)
    assert r.exact is None
    assert (r.date_from, r.date_to, r.weekdays) == (date(2026, 11, 5), None, [3])
    assert (r.hour_from, r.hour_to) == (13, 18)


def test_part_of_day_only():
    r = parse_time_request("sabah saatlerinde olursa iyi olur", CALL)
    assert (r.exact, r.date_from, r.hour_from, r.hour_to) == (None, None, 9, 12)


def test_next_week_range():
    r = parse_time_request("gelecek hafta bir toplantı", CALL)
    assert (r.date_from, r.date_to) == (date(2026, 11, 2), date(2026, 11, 8))


def test_tomorrow_without_time_is_a_fixed_day():
    r = parse_time_request("yarın olabilir", CALL)
    assert (r.date_from, r.date_to, r.exact, r.hour_from) == \
        (date(2026, 10, 31), date(2026, 10, 31), None, None)


def test_time_without_day():
    r = parse_time_request("saat 4'te müsaitim", CALL)
    assert (r.exact, r.date_from, r.hour_from, r.hour_to) == (None, None, 16, 17)


def test_nothing_said_means_no_constraints():
    r = parse_time_request("en kısa sürede görüşmek isterim", CALL)
    assert (r.exact, r.date_from, r.date_to, r.weekdays, r.hour_from) == \
        (None, None, None, None, None)
    assert "as soon as possible" in r.notes


def test_masked_labels_do_not_confuse_the_parser():
    r = parse_time_request("numaram [TELEFON MASKELENDİ], pazartesi saat 10 olur", CALL)
    assert r.exact == at(2026, 11, 2, 10)


def test_json_roundtrip_fields():
    j = parse_time_request("perşembe öğleden sonra", CALL).to_json()
    assert j["date_from"] == "2026-11-05" and j["weekdays"] == [3] and j["exact"] is None
