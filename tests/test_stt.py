"""STT providers against a mocked HTTP layer, speaker roles and channel splitting."""

from __future__ import annotations

import io
import struct
import wave

import httpx
import pytest

from app.audio import split_channels
from app.errors import PermanentError, TransientError
from app.stt import (AGENT, CUSTOMER, ChannelTranscript, FasterWhisperSTT, GroqSTT, Segment,
                     assemble, assign_roles, to_turns)

KEY = "gsk_test_key_do_not_use_123456"


def groq(handler, key=KEY, timeout=5.0) -> GroqSTT:
    return GroqSTT(key, transport=httpx.MockTransport(handler), timeout=timeout)


def ok_body(segments=(("Merhaba", 0.0, 1.0),), language="turkish"):
    return {"text": " ".join(s[0] for s in segments), "language": language, "duration": 3.0,
            "segments": [{"start": a, "end": b, "text": " " + t} for t, a, b in segments]}


def test_successful_transcription_and_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json=ok_body([("Merhaba", 0.0, 1.2), ("buyurun", 1.3, 2.0)]))

    result = groq(handler).transcribe(b"RIFF....", "rec_001_ch0.wav", "tr")
    assert [s.text for s in result.segments] == ["Merhaba", "buyurun"]
    assert result.segments[1].start == 1.3
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    for part in (b"whisper-large-v3", b'name="language"', b"tr", b"verbose_json",
                 b'filename="rec_001_ch0.wav"'):
        assert part in body


@pytest.mark.parametrize("status", [401, 403])
def test_bad_api_key_is_permanent_and_does_not_leak_the_key(status):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": f"Invalid API Key {KEY}"}})

    with pytest.raises(PermanentError) as exc:
        groq(handler).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_auth_error"
    assert f"HTTP {status}" in exc.value.message
    assert KEY not in exc.value.message and "***" in exc.value.message


def test_missing_key_fails_without_calling_the_api():
    def handler(request):
        raise AssertionError("must not be called")

    with pytest.raises(PermanentError) as exc:
        groq(handler, key=None).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_not_configured"


def test_rate_limit_is_transient_with_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "7"},
                              json={"error": {"message": "Rate limit reached"}})

    with pytest.raises(TransientError) as exc:
        groq(handler).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_rate_limited" and exc.value.retry_after == 7.0


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_transient(status):
    with pytest.raises(TransientError) as exc:
        groq(lambda r: httpx.Response(status, text="upstream error")).transcribe(
            b"x", "a.wav", "tr")
    assert exc.value.code == "stt_server_error"


@pytest.mark.parametrize("status", [400, 413, 415])
def test_rejected_requests_are_permanent(status):
    with pytest.raises(PermanentError) as exc:
        groq(lambda r: httpx.Response(status, json={"error": {"message": "file too large"}})
             ).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_rejected"


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientError) as exc:
        groq(handler, timeout=3).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_timeout" and "3s" in exc.value.message


def test_network_failure_is_transient():
    def handler(request):
        raise httpx.ConnectError("Name or service not known", request=request)

    with pytest.raises(TransientError) as exc:
        groq(handler).transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_network_error"


def test_garbage_response_is_transient():
    with pytest.raises(TransientError) as exc:
        groq(lambda r: httpx.Response(200, text="<html>proxy error</html>")).transcribe(
            b"x", "a.wav", "tr")
    assert exc.value.code == "stt_bad_response"


def test_faster_whisper_reports_missing_dependency():
    pytest.importorskip  # documented: optional dependency
    try:
        import faster_whisper  # noqa: F401
        pytest.skip("faster-whisper installed")
    except ImportError:
        pass
    with pytest.raises(PermanentError) as exc:
        FasterWhisperSTT().transcribe(b"x", "a.wav", "tr")
    assert exc.value.code == "stt_provider_unavailable"


# ------------------------------------------------------------------- roles

def ch(*segments) -> ChannelTranscript:
    segs = [Segment(a, b, t) for t, a, b in segments]
    return ChannelTranscript(segs, " ".join(s.text for s in segs))


def test_agent_detected_by_phrases_on_either_channel():
    customer = ch(("Alo, merhaba", 0.2, 1.0), ("Faturam iki kez çekildi", 3.0, 5.0))
    agent = ch(("Harbor müşteri hizmetleri, ben Zeynep, size nasıl yardımcı olabilirim?",
                1.0, 3.0))
    assert assign_roles([customer, agent]) == ({1: AGENT, 0: CUSTOMER}, "phrases")
    assert assign_roles([agent, customer]) == ({0: AGENT, 1: CUSTOMER}, "phrases")


def test_first_speaker_is_agent_when_phrases_are_inconclusive():
    a, b = ch(("Merhaba", 0.5, 1.0)), ch(("Merhaba", 1.5, 2.0))
    assert assign_roles([b, a]) == ({1: AGENT, 0: CUSTOMER}, "first_speaker")


def test_mono_has_no_roles():
    roles, method = assign_roles([ch(("Merhaba", 0, 1))])
    assert method == "mono" and roles == {0: "Konuşmacı"}


def test_assemble_orders_by_time_and_labels_speakers():
    agent = ch(("Harbor, buyurun", 0.0, 1.0), ("Not aldım", 4.0, 5.0))
    customer = ch(("Toplantı istiyorum", 1.5, 3.0), ("yarın saat üçte", 3.0, 3.8))
    t = assemble([agent, customer])
    assert t.text.splitlines() == ["Temsilci: Harbor, buyurun",
                                   "Müşteri: Toplantı istiyorum",
                                   "Müşteri: yarın saat üçte",
                                   "Temsilci: Not aldım"]
    turns = to_turns(t.segments)
    assert [x["speaker"] for x in turns] == ["Temsilci", "Müşteri", "Temsilci"]
    assert turns[1]["text"] == "Toplantı istiyorum yarın saat üçte"
    assert t.meta["roles"] == {"0": "Temsilci", "1": "Müşteri"}


# ---------------------------------------------------------- channel split

def test_split_channels_separates_left_and_right(tmp_path):
    frames = b"".join(struct.pack("<hh", 1000, -2000) for _ in range(8000))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(frames)
    path = tmp_path / "stereo.wav"
    path.write_bytes(buf.getvalue())

    left, right = split_channels(path)
    for mono, value in ((left, 1000), (right, -2000)):
        with wave.open(io.BytesIO(mono)) as w:
            assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 8000, 8000)
            assert struct.unpack("<h", w.readframes(1))[0] == value


# ------------------------------------------------- whisper silence hallucinations

from app.stt import drop_hallucinations  # noqa: E402


def _seg(text, speaker="Müşteri"):
    return {"channel": 1, "speaker": speaker, "start": 0.0, "end": 1.0, "text": text}


@pytest.mark.parametrize("text", [
    "abone ol abone ol",
    "Abone ol. Abone ol!",
    "Altyazı M.K.",
    "İzlediğiniz için teşekkürler.",
    "altyazı m.k. abone ol",
])
def test_segment_that_is_only_a_hallucination_is_dropped(text):
    assert drop_hallucinations([_seg(text)]) == []


@pytest.mark.parametrize("text, expected", [
    ("at gmail.com Evet doğrudur. Altyazı M.K.", "at gmail.com Evet doğrudur."),
    ("Altyaz M h ne demek", "h ne demek"),            # credit garbled by STT
    ("Yarın Altyazı M.K.", "Yarın"),
])
def test_subtitle_credit_is_cut_from_real_speech(text, expected):
    assert [s["text"] for s in drop_hallucinations([_seg(text)])] == [expected]


@pytest.mark.parametrize("text", [
    "aboneliğimi iptal etmek istiyorum",
    "abone olmak istiyorum, nasıl abone olurum?",
    "bu ay abone oldum ama fatura gelmedi",
    "Teşekkürler, iyi günler",
    "Altı yüz lira ödedim",
])
def test_real_speech_is_kept(text):
    assert [s["text"] for s in drop_hallucinations([_seg(text)])] == [text]


def test_other_segments_and_fields_are_untouched():
    segs = [_seg("Merhaba", "Temsilci"), _seg("abone ol abone ol"), _seg("Faturam yüksek")]
    out = drop_hallucinations(segs)
    assert [s["text"] for s in out] == ["Merhaba", "Faturam yüksek"]
    assert out[0] is not segs[0] and segs[1]["text"] == "abone ol abone ol"   # no mutation


def test_masking_step_drops_hallucinations_and_merges_turns_around_them():
    from app.steps import mask_transcript
    doc = {"segments": [_seg("Merhaba, ben Zeynep.", "Temsilci"), _seg("abone ol abone ol"),
                        _seg("Ofiste yüz yüze görüşelim", "Temsilci"),
                        _seg("Tamam. Altyazı M.K.")]}
    text, turns, _ = mask_transcript(doc)
    assert text == ("Temsilci: Merhaba, ben [İSİM MASKELENDİ]. Ofiste yüz yüze görüşelim\n"
                    "Müşteri: Tamam.")
    assert doc["segments"][1]["text"] == "abone ol abone ol"     # raw input untouched


@pytest.mark.parametrize("text, expected", [
    # seen in the real run: the hallucination glued to the start of real speech
    ("abone ol abone ol abone ol bu bir ürünümüzde hata yaşıyoruz",
     "bu bir ürünümüzde hata yaşıyoruz"),
    ("Faturam yüksek. Abone ol, abone ol!", "Faturam yüksek."),
    ("hemen abone ol dedi bana", "hemen abone ol dedi bana"),   # once: could be real speech
])
def test_repeated_subscribe_is_cut_from_real_speech(text, expected):
    assert [s["text"] for s in drop_hallucinations([_seg(text)])] == [expected]
