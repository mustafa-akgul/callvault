"""Find the call's actions in the MASKED transcript: meeting request, complaint, both,
neither. Rule-based and deterministic (see DECISIONS D-24): the output is persisted once
in crm_actions and never re-derived on retry, so a retry can never change the request
body sent to the CRM.

Input: speaker turns [{speaker, text}] from transcripts.segments (masked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.numbers_tr import tr_lower
from app.stt import AGENT, CUSTOMER
from app.timeparse_tr import TimeRequest, parse_time_request

MEETING_WORDS = (
    "toplantı", "görüşme", "randevu", "demo", "sunum", "yüz yüze", "buluşma",
    "görüşmek ist", "görüşebilir", "bir araya", "online görüş", "takvim",
)
COMPLAINT_WORDS = (
    "şikayet", "şikâyet", "memnun değil", "memnuniyetsiz", "sorun", "problem", "hata",
    "arıza", "bozuk", "çalışmıyor", "gecik", "iade", "iki kez", "fazladan", "mağdur",
    "kabul edilemez", "rezalet", "berbat", "yanlış", "eksik", "ulaşamıyorum",
    "çözülmedi", "dönüş yapılmadı", "kimse ilgilenm", "ücret kesil", "çekildi",
)
COMPLAINT_NEGATIONS = ("sorun değil", "sorun yok", "problem değil", "problem yok",
                       "sorunsuz", "hata yok", "bir sorun olmadı")
# Phrases that mention a meeting without asking for one.
MEETING_NEGATIONS = ("toplantı istemiyorum", "randevuya gerek yok", "görüşmeye gerek yok")

SUMMARY_MAX = 240


@dataclass
class Extraction:
    meeting: TimeRequest | None
    complaint_summary: str | None

    @property
    def empty(self) -> bool:
        return self.meeting is None and self.complaint_summary is None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _has(text: str, words) -> bool:
    low = tr_lower(text)
    return any(w in low for w in words)


def _customer_turns(turns: list[dict]) -> list[dict]:
    own = [t for t in turns if t.get("speaker") == CUSTOMER]
    # Mono / unknown roles: everyone except a known agent.
    return own or [t for t in turns if t.get("speaker") != AGENT]


def summarize_complaint(turns: list[dict]) -> str | None:
    picked: list[str] = []
    for turn in _customer_turns(turns):
        for sentence in _sentences(turn["text"]):
            if _has(sentence, COMPLAINT_WORDS) and not _has(sentence, COMPLAINT_NEGATIONS):
                picked.append(sentence)
    if not picked:
        return None
    summary = " ".join(picked)
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 1].rsplit(" ", 1)[0] + "…"
    return "Müşteri şikayeti: " + summary


def find_meeting(turns: list[dict], recorded_at: datetime) -> TimeRequest | None:
    start = next((i for i, t in enumerate(turns)
                  if _has(t["text"], MEETING_WORDS) and not _has(t["text"], MEETING_NEGATIONS)),
                 None)
    if start is None:
        return None
    window = turns[start:]
    customer = " ".join(t["text"] for t in window if t.get("speaker") != AGENT)
    req = parse_time_request(customer, recorded_at)
    if req.exact is None and req.date_from is None and req.hour_from is None:
        # The caller may only have said "evet, olur" to a time the agent proposed.
        agent_req = parse_time_request(" ".join(t["text"] for t in window), recorded_at)
        if agent_req.exact or agent_req.date_from or agent_req.hour_from:
            agent_req.notes.append("time taken from the whole conversation")
            req = agent_req
    req.text = " ".join(t["text"] for t in window)[:500]
    return req


def extract(turns: list[dict], recorded_at: datetime) -> Extraction:
    return Extraction(meeting=find_meeting(turns, recorded_at),
                      complaint_summary=summarize_complaint(turns))
