"""Slot selection rules (pure functions, no I/O).

  1. exact time requested and that slot is free           -> book it
  2. exact time requested but taken / not a slot          -> first free slot starting
                                                             after the requested time
  3. no exact time ("öğleden sonra", "pazartesi", nothing) -> first free slot matching
                                                             what was said
  4. nothing matches                                       -> None (needs follow-up)

Never a slot that starts before the call itself (`recorded_at`). Slots the caller's own
earlier attempts lost with 409 are excluded explicitly (`exclude`), in case the slot
list we read was stale.
"""

from __future__ import annotations

from datetime import datetime

from app.crm import Slot
from app.timeparse_tr import TR_TZ, TimeRequest


def _matches(slot_dt: datetime, req: TimeRequest) -> bool:
    local = slot_dt.astimezone(TR_TZ)
    d = local.date()
    if req.date_from and d < req.date_from:
        return False
    if req.date_to and d > req.date_to:
        return False
    if req.weekdays is not None and local.weekday() not in req.weekdays:
        return False
    if req.hour_from is not None and not (req.hour_from <= local.hour < req.hour_to):
        return False
    return True


def choose_slot(req: TimeRequest, slots: list[Slot], recorded_at: datetime,
                exclude: frozenset[str] | set[str] = frozenset()) -> tuple[Slot | None, str]:
    """Return (slot, rule) where rule explains the choice, or (None, reason)."""
    free = sorted((s for s in slots
                   if s.available and s.start not in exclude and s.start_dt >= recorded_at),
                  key=lambda s: s.start_dt)

    if req.exact is not None:
        for s in free:
            if s.start_dt == req.exact:
                return s, "exact"
        for s in free:
            if s.start_dt > req.exact:
                return s, "next_after_requested"
        return None, f"no free slot after the requested time {req.exact.isoformat()}"

    for s in free:
        if _matches(s.start_dt, req):
            return s, "first_matching"
    return None, "no free slot matches the request"
