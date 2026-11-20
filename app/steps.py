"""Step handlers. A handler does the step's work and returns a StepResult; it never
changes the state machine itself. The worker persists the result and advances the
recording in one fenced transaction (app/queue.py).

Errors (app/errors.py):
  TransientError  third party flaked (5xx, 429, timeout): retried with backoff.
  PermanentError  retrying cannot help (bad audio, bad API key): FAILED right away,
                  resumable with `reprocess` once the cause is fixed.
  anything else   treated as transient (a bug should not silently drop a recording),
                  so it ends as FAILED(unexpected_error) after max_attempts.

Data flow:
  CRM            masked transcript -> planned actions -> idempotent CRM calls
                 (app/crm_step.py)
  TRANSCRIPTION  audio -> one STT call per channel -> speaker roles -> encrypted raw
                 transcript in transcripts.raw_text_enc (never stored in clear)
  MASKING        decrypt raw -> mask per speaker turn -> masked_text / segments /
                 pii_summary. Re-runnable without calling STT again.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.crypto import DecryptionFailed, KeyMissing, TranscriptCipher
from app.db import tenant_tx
from app.errors import PermanentError, StepError, TransientError  # noqa: F401 (re-export)
from app.pii import NAME_QUESTION, mask_text
from app.queue import Job
from app.stt import (STTProvider, assemble, drop_hallucinations, provider_from_env, to_turns,
                     transcript_doc)
from app.validation import InvalidAudio, sha256_file, validate_wav


@dataclass
class StepContext:
    conn: psycopg.Connection      # worker connection, for reads via tenant_tx()
    job: Job
    recording: dict
    audio_root: Path


@dataclass
class StepResult:
    # Runs inside the fenced transaction that advances the state machine.
    persist: Callable[[psycopg.Connection], None] | None = None
    duplicate_of: str | None = None
    notes: dict = field(default_factory=dict)


Handler = Callable[[StepContext], StepResult]


def audio_path(ctx: StepContext) -> Path:
    path = (ctx.audio_root / ctx.recording["file_path"]).resolve()
    root = ctx.audio_root.resolve()
    if root not in path.parents:
        raise PermanentError("bad_path", "recording path escapes the audio directory")
    return path


def _aad(ctx: StepContext) -> str:
    return f"{ctx.job.tenant_id}/{ctx.job.recording_id}"


# ------------------------------------------------------------------- VALIDATION

def find_canonical(conn: psycopg.Connection, recording: dict, audio_hash: str) -> str | None:
    """Earliest recording of the same tenant with the same audio, if not this one.

    Ordering by (recorded_at, id) makes the choice independent of which worker happens
    to validate first: rec_001 (08:49) is canonical, rec_021 (12:46) its duplicate.
    """
    with tenant_tx(conn, recording["tenant_id"]):
        row = conn.execute(
            "SELECT id FROM recordings WHERE audio_hash = %s AND (recorded_at, id) < (%s, %s)"
            " ORDER BY recorded_at, id LIMIT 1",
            (audio_hash, recording["recorded_at"], recording["id"]),
        ).fetchone()
    return row["id"] if row else None


def validation_step(ctx: StepContext) -> StepResult:
    path = audio_path(ctx)
    try:
        validate_wav(path)
    except InvalidAudio as exc:
        raise PermanentError(exc.code, exc.message) from None

    audio_hash = ctx.recording["audio_hash"]
    persist = None
    if audio_hash is None:  # file appeared after ingest
        audio_hash, size = sha256_file(path)

        def persist(conn: psycopg.Connection, h=audio_hash, n=size) -> None:
            conn.execute(
                "UPDATE recordings SET audio_hash = %s, audio_bytes = %s"
                " WHERE tenant_id = %s AND id = %s",
                (h, n, ctx.job.tenant_id, ctx.job.recording_id),
            )

    return StepResult(persist=persist, duplicate_of=find_canonical(ctx.conn, ctx.recording,
                                                                   audio_hash))


# ---------------------------------------------------------------- TRANSCRIPTION

def make_transcription_step(get_stt: Callable[[], STTProvider],
                            get_cipher: Callable[[], TranscriptCipher]) -> Handler:
    def transcription_step(ctx: StepContext) -> StepResult:
        from app.audio import split_channels

        cipher = get_cipher()
        stt = get_stt()
        try:
            channels_wav = split_channels(audio_path(ctx))
        except (OSError, ValueError, EOFError) as exc:
            raise PermanentError("audio_decode_error", f"{type(exc).__name__}: {exc}") from None

        results = []
        for i, wav in enumerate(channels_wav):
            try:
                results.append(stt.transcribe(wav, f"{ctx.job.recording_id}_ch{i}.wav",
                                              ctx.recording.get("language")))
            except StepError as exc:
                exc.message = f"[{stt.name}/{stt.model}, channel {i + 1}/{len(channels_wav)}]" \
                              f" {exc.message}"
                raise

        transcript = assemble(results)
        if not transcript.text.strip():
            raise PermanentError("empty_transcript",
                                 f"{stt.name} returned no speech for any of "
                                 f"{len(channels_wav)} channel(s)")
        blob = cipher.encrypt(json.dumps(transcript_doc(transcript), ensure_ascii=False)
                              .encode(), _aad(ctx))
        language = (transcript.meta["languages"] or [ctx.recording.get("language")])[0]

        def persist(conn: psycopg.Connection) -> None:
            # Re-transcribing invalidates any earlier masking of the old text.
            conn.execute(
                """
                INSERT INTO transcripts (tenant_id, recording_id, provider, model, language,
                                         raw_text_enc, stt_meta, transcribed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (tenant_id, recording_id) DO UPDATE SET
                    provider = EXCLUDED.provider, model = EXCLUDED.model,
                    language = EXCLUDED.language, raw_text_enc = EXCLUDED.raw_text_enc,
                    stt_meta = EXCLUDED.stt_meta, transcribed_at = now(),
                    masked_text = NULL, segments = NULL, pii_summary = NULL,
                    masked_at = NULL, updated_at = now()
                """,
                (ctx.job.tenant_id, ctx.job.recording_id, stt.name, stt.model, language,
                 blob, Jsonb(transcript.meta)),
            )

        return StepResult(persist=persist, notes={"segments": len(transcript.segments)})

    return transcription_step


# ---------------------------------------------------------------------- MASKING

def mask_transcript(doc: dict) -> tuple[str, list[dict], Counter]:
    counts: Counter = Counter()
    turns = []
    # The raw transcript keeps everything STT returned; derived text drops hallucinations.
    prev = None
    for turn in to_turns(drop_hallucinations(doc["segments"])):
        asked = bool(prev and prev["speaker"] != turn["speaker"]
                     and NAME_QUESTION.search(prev["text"]))
        result = mask_text(turn["text"], asked_name=asked)
        prev = turn
        counts.update(result.counts)
        turns.append({**turn, "text": result.text})
    masked_text = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)
    return masked_text, turns, counts


def make_masking_step(get_cipher: Callable[[], TranscriptCipher]) -> Handler:
    def masking_step(ctx: StepContext) -> StepResult:
        cipher = get_cipher()
        with tenant_tx(ctx.conn, ctx.job.tenant_id):
            row = ctx.conn.execute(
                "SELECT raw_text_enc FROM transcripts WHERE tenant_id = %s AND recording_id = %s",
                (ctx.job.tenant_id, ctx.job.recording_id),
            ).fetchone()
        if row is None or row["raw_text_enc"] is None:
            raise PermanentError("transcript_missing",
                                 "no raw transcript stored; reprocess from TRANSCRIPTION")
        try:
            doc = json.loads(cipher.decrypt(row["raw_text_enc"], _aad(ctx)))
        except DecryptionFailed as exc:
            raise PermanentError("decrypt_failed", str(exc)) from None
        try:
            masked_text, turns, counts = mask_transcript(doc)
        except Exception as exc:  # deterministic: retrying the same input cannot help
            raise PermanentError("masking_error",
                                 f"{type(exc).__name__}: {exc}"[:500]) from None

        def persist(conn: psycopg.Connection) -> None:
            conn.execute(
                "UPDATE transcripts SET masked_text = %s, segments = %s, pii_summary = %s,"
                " masked_at = now(), updated_at = now()"
                " WHERE tenant_id = %s AND recording_id = %s",
                (masked_text, Jsonb(turns), Jsonb(dict(counts)),
                 ctx.job.tenant_id, ctx.job.recording_id),
            )

        return StepResult(persist=persist, notes=dict(counts))

    return masking_step


# --------------------------------------------------------------------- registry

def not_implemented(step: str) -> Handler:
    def handler(ctx: StepContext) -> StepResult:
        raise PermanentError("step_not_implemented",
                             f"{step} is not implemented yet; run `reprocess` once it is")
    return handler


def build_handlers(stt: STTProvider | None = None,
                   cipher: TranscriptCipher | None = None,
                   crm=None) -> dict[str, Handler]:
    """Dependencies are created lazily, on first use, so a worker without an STT key
    still validates audio and reports the missing key as a per-recording failure."""
    from app.crm_step import crm_from_env, make_crm_step  # imports StepResult from here

    cache: dict = {"stt": stt, "cipher": cipher, "crm": crm}

    def get_crm():
        if cache["crm"] is None:
            cache["crm"] = crm_from_env()
        return cache["crm"]

    def get_stt() -> STTProvider:
        if cache["stt"] is None:
            cache["stt"] = provider_from_env()
        return cache["stt"]

    def get_cipher() -> TranscriptCipher:
        if cache["cipher"] is None:
            try:
                cache["cipher"] = TranscriptCipher.from_env()
            except (KeyMissing, ValueError) as exc:
                raise PermanentError("encryption_key_missing", str(exc)) from None
        return cache["cipher"]

    return {
        "VALIDATION": validation_step,
        "TRANSCRIPTION": make_transcription_step(get_stt, get_cipher),
        "MASKING": make_masking_step(get_cipher),
        "CRM": make_crm_step(get_crm),
    }


def default_handlers() -> dict[str, Handler]:
    return build_handlers()
