"""Generate calendar.json: one-hour slots, 10:00-17:00, weekdays, with a few pre-booked.

The pre-booked slots exist so the booking rules (exact / next free / follow-up) have
something to collide with. Re-running this script rewrites calendar.json deterministically.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=3))
FIRST_DAY = date(2026, 10, 30)
DAYS = 15
PREBOOKED = {
    "2026-10-30T13:00", "2026-11-02T12:00", "2026-11-02T13:00",
    "2026-11-03T16:00", "2026-11-03T17:00", "2026-11-05T10:00",
}


def build() -> dict:
    slots = []
    for offset in range(DAYS):
        day = FIRST_DAY + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour in range(10, 18):
            start = datetime(day.year, day.month, day.day, hour, tzinfo=TZ)
            end = start + timedelta(hours=1)
            slots.append({
                "start": start.isoformat(),
                "end": end.isoformat(),
                "available": start.strftime("%Y-%m-%dT%H:%M") not in PREBOOKED,
            })
    return {"timezone": "Europe/Istanbul", "slots": slots}


if __name__ == "__main__":
    out = Path(__file__).with_name("calendar.json")
    calendar = build()
    out.write_text(json.dumps(calendar, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(calendar['slots'])} slots)")
