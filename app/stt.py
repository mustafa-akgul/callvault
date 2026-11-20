"""Speech-to-text providers and transcript assembly.

Providers implement `transcribe(wav_bytes, filename, language) -> ChannelTranscript`
and raise TransientError / PermanentError, never raw HTTP or library exceptions:

  Groq (primary)          401/403 -> PermanentError stt_auth_error   (bad / revoked key)
                          400/404/413/415/422 -> PermanentError stt_rejected
                          429 -> TransientError stt_rate_limited (honours Retry-After)
                          5xx -> TransientError stt_server_error
                          timeout -> TransientError stt_timeout
                          connection errors -> TransientError stt_network_error
  faster-whisper (local)  same interface; only loaded if STT_PROVIDER=faster_whisper.

There is deliberately no automatic fallback from Groq to local: a broken key must show
up as FAILED(TRANSCRIPTION, stt_auth_error), not be hidden behind a slower, different
model. Switching provider is an explicit configuration change.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.errors import PermanentError, TransientError
from app.numbers_tr import tr_lower

AGENT = "Temsilci"
CUSTOMER = "Müşteri"
UNKNOWN = "Konuşmacı"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class ChannelTranscript:
    segments: list[Segment]
    text: str
    language: str | None = None
    duration: float | None = None


class STTProvider(Protocol):
    name: str
    model: str

    def transcribe(self, wav: bytes, filename: str, language: str | None) -> ChannelTranscript:
        ...


# ------------------------------------------------------------------------- Groq

class GroqSTT:
    name = "groq"
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_key: str | None, model: str = "whisper-large-v3",
                 url: str | None = None, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None):
        self._key = api_key or ""
        self.model = model
        self._url = url or self.URL
        self._timeout = timeout
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                    transport=transport)

    def _scrub(self, s: str) -> str:
        return s.replace(self._key, "***") if self._key else s

    def transcribe(self, wav: bytes, filename: str, language: str | None) -> ChannelTranscript:
        if not self._key:
            raise PermanentError("stt_not_configured",
                                 "GROQ_API_KEY is not set (see .env.example)")
        data = {"model": self.model, "response_format": "verbose_json", "temperature": "0",
                "timestamp_granularities[]": "segment"}
        if language:
            data["language"] = language
        try:
            resp = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                data=data,
                files={"file": (filename, wav, "audio/wav")},
            )
        except httpx.TimeoutException:
            raise TransientError("stt_timeout",
                                 f"Groq did not answer within {self._timeout:.0f}s "
                                 f"({filename})") from None
        except httpx.TransportError as exc:
            raise TransientError("stt_network_error",
                                 self._scrub(f"{type(exc).__name__}: {exc} ({filename})")
                                 ) from None

        if resp.status_code != 200:
            detail = self._error_detail(resp)
            where = f"Groq HTTP {resp.status_code} for {filename}: {detail}"
            if resp.status_code in (401, 403):
                raise PermanentError("stt_auth_error", where)
            if resp.status_code == 429:
                raise TransientError("stt_rate_limited", where,
                                     retry_after=_retry_after(resp.headers.get("retry-after")))
            if resp.status_code >= 500:
                raise TransientError("stt_server_error", where)
            raise PermanentError("stt_rejected", where)

        try:
            body = resp.json()
            segments = [Segment(float(s["start"]), float(s["end"]), s["text"].strip())
                        for s in body.get("segments") or []]
            text = body.get("text", "").strip()
        except (ValueError, KeyError, TypeError) as exc:
            raise TransientError("stt_bad_response",
                                 f"unexpected Groq response for {filename}: "
                                 f"{type(exc).__name__}") from None
        if not segments and text:
            segments = [Segment(0.0, float(body.get("duration") or 0.0), text)]
        return ChannelTranscript(segments, text, body.get("language"), body.get("duration"))

    def _error_detail(self, resp: httpx.Response) -> str:
        try:
            msg = resp.json().get("error", {}).get("message") or resp.text
        except ValueError:
            msg = resp.text
        return self._scrub(str(msg))[:300]


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


# --------------------------------------------------------------- faster-whisper

class FasterWhisperSTT:
    """Local alternative (no network, no key). Heavy: not installed by default.

    pip install faster-whisper, then STT_PROVIDER=faster_whisper. The model is loaded
    lazily on first use and kept for the life of the worker.
    """

    name = "faster_whisper"

    def __init__(self, model: str = "large-v3", device: str = "cpu",
                 compute_type: str = "int8"):
        self.model = model
        self._device, self._compute_type = device, compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise PermanentError("stt_provider_unavailable",
                                     "faster-whisper is not installed "
                                     "(pip install faster-whisper)") from None
            self._model = WhisperModel(self.model, device=self._device,
                                       compute_type=self._compute_type)
        return self._model

    def transcribe(self, wav: bytes, filename: str, language: str | None) -> ChannelTranscript:
        model = self._load()
        try:
            segs, info = model.transcribe(io.BytesIO(wav), language=language, temperature=0,
                                          vad_filter=True)
            segments = [Segment(s.start, s.end, s.text.strip()) for s in segs]
        except MemoryError:
            raise TransientError("stt_out_of_memory", f"local model ran out of memory "
                                                      f"({filename})") from None
        text = " ".join(s.text for s in segments)
        return ChannelTranscript(segments, text, info.language, info.duration)


def provider_from_env() -> STTProvider:
    provider = os.environ.get("STT_PROVIDER", "groq")
    if provider == "groq":
        return GroqSTT(os.environ.get("GROQ_API_KEY"),
                       model=os.environ.get("GROQ_STT_MODEL", "whisper-large-v3"),
                       timeout=float(os.environ.get("STT_TIMEOUT_SECONDS", "120")))
    if provider == "faster_whisper":
        return FasterWhisperSTT(model=os.environ.get("FASTER_WHISPER_MODEL", "large-v3"))
    raise PermanentError("stt_not_configured", f"unknown STT_PROVIDER={provider!r}")


# ---------------------------------------------------------- speaker assignment

AGENT_PHRASES = (
    "harbor", "hoş geldiniz", "nasıl yardımcı", "yardımcı olabilirim", "size nasıl",
    "müşteri hizmetleri", "aradığınız için", "iyi günler dilerim", "başka bir konuda",
    "kaydınızı", "talebinizi", "oluşturuyorum", "kontrol ediyorum",
)


@dataclass
class Transcript:
    segments: list[dict]                       # [{channel, speaker, start, end, text}]
    roles: dict[int, str]                      # channel -> speaker
    role_method: str
    text: str
    meta: dict = field(default_factory=dict)


def assign_roles(channels: list[ChannelTranscript]) -> tuple[dict[int, str], str]:
    """Which channel is the agent? Score greeting / service phrases in the first
    segments; if that is inconclusive, the channel that speaks first is the agent
    (the call center answers the phone)."""
    if len(channels) == 1:
        return {0: UNKNOWN}, "mono"
    if len(channels) != 2:
        return {i: f"{UNKNOWN} {i + 1}" for i in range(len(channels))}, "multi"

    def score(ch: ChannelTranscript) -> int:
        opening = tr_lower(" ".join(s.text for s in ch.segments[:4]))
        return sum(opening.count(p) for p in AGENT_PHRASES)

    s0, s1 = score(channels[0]), score(channels[1])
    if s0 != s1:
        agent = 0 if s0 > s1 else 1
        method = "phrases"
    else:
        firsts = [ch.segments[0].start if ch.segments else float("inf") for ch in channels]
        agent = 0 if firsts[0] <= firsts[1] else 1
        method = "first_speaker"
    return {agent: AGENT, 1 - agent: CUSTOMER}, method


def assemble(channels: list[ChannelTranscript]) -> Transcript:
    roles, method = assign_roles(channels)
    segments = sorted(
        ({"channel": i, "speaker": roles[i], "start": round(s.start, 2),
          "end": round(s.end, 2), "text": s.text}
         for i, ch in enumerate(channels) for s in ch.segments if s.text),
        key=lambda s: (s["start"], s["channel"]),
    )
    text = "\n".join(f"{s['speaker']}: {s['text']}" for s in segments)
    meta = {"channels": len(channels), "roles": {str(k): v for k, v in roles.items()},
            "role_method": method,
            "duration": max((c.duration or 0.0) for c in channels) if channels else 0.0,
            "languages": sorted({c.language for c in channels if c.language})}
    return Transcript(segments, roles, method, text, meta)


# Whisper was trained on subtitled video; on silence or noise it emits the credits it saw
# there. Seen in the real calls: "abone ol" (8 recordings), "Altyazı M.K." (also garbled
# to "Altyaz M"), and the classic "İzlediğiniz için teşekkürler".
_CREDIT = re.compile(r"\s*\baltyaz[ıi]?\s*m\b\.?(?:\s*k\b\.?)?", re.IGNORECASE)
# Two or more "abone ol" in a row is the hallucination even inside real speech (seen in
# the real run: "abone ol abone ol abone ol bu bir ürünümüzde hata yaşıyoruz").
_REPEATED = re.compile(r"(?:\babone\s+ol(?:un)?\b[\s,.!]*){2,}", re.IGNORECASE)
_SILENCE_ONLY = re.compile(
    r"^(?:\s*(?:abone\s+ol(?:un)?|altyaz[ıi]?\s*m\.?(?:\s*k\.?)?"
    r"|izlediğiniz\s+için\s+teşekkür(?:ler|\s+ederiz|\s+ederim))[\s.,!?]*)+$",
    re.IGNORECASE)


def _tr_casefold(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def drop_hallucinations(segments: list[dict]) -> list[dict]:
    """Remove subtitle-credit hallucinations from STT segments (returns new dicts).

    Conservative on purpose: a segment is dropped only when it consists of nothing but
    such phrases; inside real speech only the subtitle credit is cut, because no caller
    says "Altyazı M.K.", while "abone olmak istiyorum" is a real request."""
    out = []
    for s in segments:
        text = s["text"]
        if _SILENCE_ONLY.match(_tr_casefold(text)):
            continue
        cleaned = _REPEATED.sub(" ", _CREDIT.sub("", text)).strip()
        if cleaned:
            out.append({**s, "text": cleaned})
    return out


def to_turns(segments: list[dict]) -> list[dict]:
    """Merge consecutive segments of the same speaker. Masking runs per turn, so a
    number dictated across several STT segments is still seen as one number."""
    turns: list[dict] = []
    for s in segments:
        if turns and turns[-1]["speaker"] == s["speaker"]:
            turns[-1]["text"] += " " + s["text"]
            turns[-1]["end"] = s["end"]
        else:
            turns.append(dict(s))
    return turns


def transcript_doc(t: Transcript) -> dict:
    return {"text": t.text, "segments": t.segments, "meta": t.meta}
