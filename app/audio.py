"""Split a (validated) PCM WAV into one mono WAV per channel.

The call recordings are stereo with one speaker per channel (agent / customer), so
transcribing each channel separately gives speaker attribution without a diarization
model.
"""

from __future__ import annotations

import array
import io
import sys
import wave
from pathlib import Path


def split_channels(path: str | Path) -> list[bytes]:
    with wave.open(str(path), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM, got {width * 8}-bit")
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        samples.byteswap()

    out = []
    for ch in range(channels):
        mono = samples[ch::channels]
        if sys.byteorder == "big":
            mono.byteswap()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as m:
            m.setnchannels(1)
            m.setsampwidth(2)
            m.setframerate(rate)
            m.writeframes(mono.tobytes())
        out.append(buf.getvalue())
    return out
