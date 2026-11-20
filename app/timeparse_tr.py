"""Turn what a Turkish caller said about time into booking constraints.

Input is the masked transcript text (numbers already normalised to digits by the
masking step, e.g. "perşembe saat 15'te"). Relative expressions are resolved against
the call's `recorded_at`, never the wall clock. Turkey is UTC+3 all year (no DST since
2016), so a fixed offset is used instead of a tz database.

Result:
  exact      a single datetime ("perşembe saat 3'te", "3 Kasım 14:00")
  date_from / date_to, weekdays, hour_from / hour_to    flexible constraints
  When several days or times are mentioned, the LAST mention wins: callers negotiate
  ("salı olmaz, perşembe olur").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from app.numbers_tr import tr_lower

TR_TZ = timezone(timedelta(hours=3))

WEEKDAYS = {"pazartesi": 0, "salı": 1, "çarşamba": 2, "perşembe": 3, "cumartesi": 5,
            "cuma": 4, "pazar": 6}
MONTHS = {"ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
          "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12}
# Longest phrases first; hours are [from, to) for the slot START.
PARTS_OF_DAY = [
    ("öğleden sonra", 13, 18), ("öğleden önce", 9, 12), ("akşamüstü", 16, 18),
    ("akşam üstü", 16, 18), ("öğle saat", 12, 14), ("öğlen", 12, 14), ("öğle", 12, 14),
    ("sabah", 9, 12), ("akşam", 17, 24), ("mesai sonu", 16, 18),
]

_WD = r"(pazartesi|salı|çarşamba|perşembe|cumartesi|cuma|pazar)(?!\w*tesi)\w*"
_MONTH = r"(ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık)\w*"
_DATE_DM = re.compile(r"\b(\d{1,2})(?:'\w+)?\s+" + _MONTH)
_DATE_MD = re.compile(r"\b" + _MONTH + r"(?:'\w+)?\s+(\d{1,2})(?:'\w+)?(?!\d)")
_WEEKDAY = re.compile(r"\b(?:(haftaya|gelecek hafta|önümüzdeki hafta|bu hafta)\s+)?" + _WD)
_RELATIVE = re.compile(r"\b(bugün|yarından sonra|öbür gün|ertesi gün|yarın)\w*")
_WEEK = re.compile(r"\b(haftaya|gelecek hafta|önümüzdeki hafta|bu hafta)\b(?!\s+" + _WD + ")")
_TIME = re.compile(
    r"\bsaat\s+(\d{1,2})(?:[:.](\d{2})|\s+(\d{2})(?!\d))?(?:'\w+)?(\s+buçuk)?"
    r"|\b(\d{1,2})[:.](\d{2})\b"
)
_PART_HOUR = re.compile(
    r"\b(öğleden sonra|öğleden önce|sabah|akşam|öğlen)\s+(\d{1,2})(?:[:.](\d{2}))?(?:'\w+)?(?!\d)"
)
_ASAP = re.compile(r"en erken|en kısa sürede|ilk müsait|en yakın|müsait olduğunuz ilk")


@dataclass
class TimeRequest:
    text: str
    exact: datetime | None = None
    date_from: date | None = None
    date_to: date | None = None
    weekdays: list[int] | None = None
    hour_from: int | None = None
    hour_to: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("exact", "date_from", "date_to"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


def _business_hour(h: int) -> int:
    # "saat 3" on a business call means 15:00 (office hours are 10-18).
    return h + 12 if 1 <= h <= 7 else h


def _next_weekday(ref: date, wd: int) -> date:
    ahead = (wd - ref.weekday()) % 7
    return ref + timedelta(days=ahead or 7)


def _next_monday(ref: date) -> date:
    return ref + timedelta(days=7 - ref.weekday())


def parse_time_request(text: str, recorded_at: datetime) -> TimeRequest:
    ref_dt = recorded_at.astimezone(TR_TZ)
    ref = ref_dt.date()
    low = tr_lower(text)
    req = TimeRequest(text=text)

    # ---- day: last mention wins
    day_hits: list[tuple[int, str, object]] = []
    for m in _RELATIVE.finditer(low):
        word = m.group(1)
        d = {"bugün": ref, "yarın": ref + timedelta(days=1)}.get(word, ref + timedelta(days=2))
        day_hits.append((m.start(), "date", d))
    for m in _WEEKDAY.finditer(low):
        week, wd = m.group(1), WEEKDAYS[m.group(2)]
        if week in ("haftaya", "gelecek hafta", "önümüzdeki hafta"):
            d = _next_monday(ref) + timedelta(days=wd)
            day_hits.append((m.start(), "date", d))
        elif week == "bu hafta":
            d = ref + timedelta(days=(wd - ref.weekday()) % 7)
            day_hits.append((m.start(), "date", d))
        else:
            day_hits.append((m.start(), "weekday", (_next_weekday(ref, wd), wd)))
    for m in _WEEK.finditer(low):
        if m.group(1) == "bu hafta":
            day_hits.append((m.start(), "range", (ref, ref + timedelta(days=6 - ref.weekday()))))
        else:
            mon = _next_monday(ref)
            day_hits.append((m.start(), "range", (mon, mon + timedelta(days=6))))
    for rx, day_i, mon_i in ((_DATE_DM, 1, 2), (_DATE_MD, 2, 1)):
        for m in rx.finditer(low):
            day, month = int(m.group(day_i)), MONTHS[m.group(mon_i)]
            try:
                d = date(ref.year, month, day)
            except ValueError:
                continue
            if d < ref - timedelta(days=30):   # "5 Ocak" said in November = next year
                d = date(ref.year + 1, month, day)
            day_hits.append((m.start(), "date", d))

    if day_hits:
        _, kind, value = max(day_hits, key=lambda h: h[0])
        if kind == "date":
            req.date_from = req.date_to = value
        elif kind == "weekday":
            first, wd = value
            req.date_from, req.weekdays = first, [wd]
        else:
            req.date_from, req.date_to = value

    # ---- time of day: last mention wins
    hours: list[tuple[int, int, int]] = []          # (pos, hour, minute)
    for m in _TIME.finditer(low):
        if m.group(1) is not None:
            h = int(m.group(1))
            minute = int(m.group(2) or m.group(3) or 0) if not m.group(4) else 30
        else:
            h, minute = int(m.group(5)), int(m.group(6))
        if h > 23 or minute > 59:
            continue
        hours.append((m.start(), _business_hour(h), minute))
    # "öğleden sonra 4'te", "sabah 10'da": a clock time without the word "saat".
    for m in _PART_HOUR.finditer(low):
        h, minute = int(m.group(2)), int(m.group(3) or 0)
        if h <= 23 and minute <= 59 and not any(p == m.start(2) for p, _, _ in hours):
            hours.append((m.start(2), _business_hour(h), minute))
    hours.sort()
    parts = []
    taken = set()
    for phrase, a, b in PARTS_OF_DAY:
        for m in re.finditer(r"\b" + phrase, low):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.add((m.start(), m.end()))
            parts.append((m.start(), a, b, phrase))

    if hours:  # an explicit clock time beats "öğleden sonra" ("öğleden sonra 3'te" = 15:00)
        _, h, minute = hours[-1]
        single_day = req.date_from is not None and (req.date_to == req.date_from
                                                    or req.weekdays is not None)
        if single_day:
            req.exact = datetime.combine(req.date_from, time(h, minute), TR_TZ)
        else:
            req.hour_from, req.hour_to = h, h + 1
            req.notes.append("time given without a single day")
    elif parts:
        _, a, b, phrase = max(parts, key=lambda p: p[0])
        req.hour_from, req.hour_to = a, b
        req.notes.append(f"part of day: {phrase}")

    if _ASAP.search(low):
        req.notes.append("as soon as possible")
    return req
