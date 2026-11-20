from __future__ import annotations

import pytest

from app.validation import InvalidAudio, validate_wav
from tests.conftest import REPO
from tests.wavs import write_truncated_wav, write_wav

SAMPLES = REPO / "samples"


def test_valid_wav_passes(tmp_path):
    info = validate_wav(write_wav(tmp_path / "ok.wav", seconds=2))
    assert info.channels == 2 and info.sample_rate == 8000
    assert info.duration_s == pytest.approx(2.0)


@pytest.mark.parametrize(
    "make, code",
    [
        (lambda p: write_truncated_wav(p, keep_bytes=2048, seconds=10), "truncated_audio"),
        (lambda p: write_wav(p, seconds=3, amplitude=5), "silent_audio"),
        (lambda p: write_wav(p, seconds=0.3), "too_short"),
        (lambda p: p.write_bytes(b"ID3\x03not a wav at all") or p, "not_wav"),
        (lambda p: p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE") or p, "bad_header"),
        (lambda p: p, "file_missing"),
    ],
)
def test_broken_audio_is_rejected(tmp_path, make, code):
    path = tmp_path / "x.wav"
    make(path)
    with pytest.raises(InvalidAudio) as exc:
        validate_wav(path)
    assert exc.value.code == code


def test_sample_truncated_recording_is_detected():
    with pytest.raises(InvalidAudio) as exc:
        validate_wav(SAMPLES / "call_06_truncated.wav")
    assert exc.value.code == "truncated_audio"


@pytest.mark.parametrize("name", ["call_01", "call_02", "call_03", "call_04"])
def test_sample_recordings_pass(name):
    info = validate_wav(SAMPLES / f"{name}.wav")
    assert info.channels == 2 and info.duration_s == pytest.approx(4.0)
