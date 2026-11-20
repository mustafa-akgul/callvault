"""Synthetic WAV files for tests (no real call audio needed)."""

from __future__ import annotations

import math
import struct
from pathlib import Path


def wav_bytes(seconds: float = 2.0, amplitude: int = 3000, rate: int = 8000,
              channels: int = 2, freq: float = 440.0) -> bytes:
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        s = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", s) * channels
    data = bytes(frames)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                    rate * channels * 2, channels * 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


def write_wav(path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(**kwargs))
    return path


def write_truncated_wav(path: Path, keep_bytes: int = 2048, **kwargs) -> Path:
    """Header claims the full length; the file stops after keep_bytes (like rec_020)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(**kwargs)[:keep_bytes])
    return path
