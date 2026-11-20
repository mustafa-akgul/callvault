from datetime import datetime

from app.extraction import SUMMARY_MAX, extract
from app.timeparse_tr import TR_TZ

CALL = datetime(2026, 10, 30, 9, 0, tzinfo=TR_TZ)


def turns(*pairs):
    return [{"speaker": s, "text": t} for s, t in pairs]


def test_meeting_and_complaint():
    t = turns(
        ("Temsilci", "Harbor, buyurun."),
        ("Müşteri", "Merhaba, adım [İSİM MASKELENDİ]. Faturam 2 kez çekildi, iade istiyorum."),
        ("Temsilci", "Anlıyorum, kaydı açıyorum."),
        ("Müşteri", "Bir de ekibinizle toplantı yapmak istiyorum, pazartesi saat 15'te olur mu?"),
    )
    e = extract(t, CALL)
    assert e.complaint_summary == "Müşteri şikayeti: Faturam 2 kez çekildi, iade istiyorum."
    assert e.meeting is not None
    assert e.meeting.exact == datetime(2026, 11, 2, 15, tzinfo=TR_TZ)


def test_neither():
    t = turns(("Temsilci", "Harbor, buyurun."), ("Müşteri", "Sadece bilgi almak istedim, teşekkürler."))
    assert extract(t, CALL).empty


def test_no_problem_is_not_a_complaint():
    t = turns(("Müşteri", "Sorun değil, toplantı için arıyorum, perşembe öğleden sonra."))
    e = extract(t, CALL)
    assert e.complaint_summary is None
    assert e.meeting.weekdays == [3] and e.meeting.hour_from == 13


def test_agent_proposed_time_accepted_by_customer():
    t = turns(
        ("Müşteri", "Sizinle bir görüşme ayarlamak istiyorum."),
        ("Temsilci", "Salı saat 11 uygun mu?"),
        ("Müşteri", "Evet, olur."),
    )
    e = extract(t, CALL)
    assert e.meeting.exact == datetime(2026, 11, 3, 11, tzinfo=TR_TZ)
    assert "time taken from the whole conversation" in e.meeting.notes


def test_agent_words_are_not_the_customers_complaint():
    t = turns(("Temsilci", "Sistemde bir sorun görünüyor mu kontrol ediyorum."),
              ("Müşteri", "Tamam, teşekkürler."))
    assert extract(t, CALL).complaint_summary is None


def test_summary_is_capped():
    long = "Kargo gecikti ve " + "çok uzun bir açıklama " * 30 + "."
    e = extract(turns(("Müşteri", long)), CALL)
    assert len(e.complaint_summary) <= SUMMARY_MAX + len("Müşteri şikayeti: ")
    assert e.complaint_summary.endswith("…")


def test_summary_keeps_masks_not_pii():
    e = extract(turns(("Müşteri", "İadeyi [IBAN MASKELENDİ] hesabıma yapmadınız, şikayetçiyim.")),
                CALL)
    assert "[IBAN MASKELENDİ]" in e.complaint_summary
