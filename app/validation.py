"""Audio pre-validation: catch broken files before they reach speech-to-text.

A truncated or silent file must fail loudly. Sent to Whisper, silence can come back as
a hallucinated transcript, which would then turn into a fake CRM action.

Checks (all on the raw bytes, no audio library needed):
  * file exists and is readable
  * RIFF/WAVE container with a PCM `fmt ` chunk and a `data` chunk
  * the `data` chunk is complete (declared size <= bytes actually present)
  * duration >= MIN_DURATION_S
  * not silent: RMS over all samples >= SILENCE_RMS (16-bit scale)

Measured on this dataset: real calls have RMS 1694..2000; rec_020 has 9.2.
"""

from __future__ import annotations

import array
import hashlib
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_DURATION_S = 1.0
SILENCE_RMS = 50.0


@dataclass(frozen=True)
class AudioInfo:
    channels: int
    sample_rate: int
    bits_per_sample: int
    duration_s: float
    rms: float


class InvalidAudio(Exception):
    """Permanent: retrying will not fix the file."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_file(path: str | Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _rms_16bit(pcm: bytes) -> float:
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % 2)
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def validate_wav(path: str | Path) -> AudioInfo:
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise InvalidAudio("file_missing", f"audio file not found: {path.name}") from None
    except OSError as exc:
        raise InvalidAudio("file_unreadable", f"cannot read {path.name}: {exc}") from None

    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise InvalidAudio("not_wav", f"{path.name} is not a RIFF/WAVE file")

    fmt = None
    data_offset = data_declared = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or body + 16 > len(data):
                raise InvalidAudio("bad_header", f"{path.name}: truncated fmt chunk")
            audio_format, channels, rate, _, _, bits = struct.unpack(
                "<HHIIHH", data[body : body + 16]
            )
            fmt = (audio_format, channels, rate, bits)
        elif chunk_id == b"data":
            data_offset, data_declared = body, chunk_size
            break
        pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned

    if fmt is None:
        raise InvalidAudio("bad_header", f"{path.name}: no fmt chunk")
    if data_offset is None:
        raise InvalidAudio("bad_header", f"{path.name}: no data chunk")

    audio_format, channels, rate, bits = fmt
    if audio_format != 1 or bits != 16 or channels < 1 or rate <= 0:
        raise InvalidAudio(
            "unsupported_format",
            f"{path.name}: need 16-bit PCM, got format={audio_format} bits={bits} "
            f"channels={channels} rate={rate}",
        )

    actual = len(data) - data_offset
    if actual < data_declared:
        raise InvalidAudio(
            "truncated_audio",
            f"{path.name}: header declares {data_declared} bytes of audio "
            f"({data_declared / (rate * channels * 2):.1f}s), file contains {actual} "
            f"({actual / (rate * channels * 2):.1f}s)",
        )

    pcm = data[data_offset : data_offset + data_declared]
    duration = len(pcm) / (rate * channels * 2)
    if duration < MIN_DURATION_S:
        raise InvalidAudio("too_short", f"{path.name}: {duration:.2f}s of audio")

    rms = _rms_16bit(pcm)
    if rms < SILENCE_RMS:
        raise InvalidAudio("silent_audio", f"{path.name}: RMS {rms:.1f} < {SILENCE_RMS}")

    return AudioInfo(channels, rate, bits, duration, rms)
