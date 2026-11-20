"""PII masking edge cases. All identifiers below are synthetic test values."""

from __future__ import annotations

import random

import pytest

from app.pii import LABELS, iban_valid, luhn_valid, mask_text, tckn_valid

NAME, PHONE, TCKN, IBAN, CARD, EMAIL = (LABELS[k] for k in
                                        ("NAME", "PHONE", "TCKN", "IBAN", "CARD", "EMAIL"))

VALID_TCKN = "10000000146"


def make_tckn(seed: int) -> str:
    rnd = random.Random(seed)
    n = [rnd.randint(1, 9)] + [rnd.randint(0, 9) for _ in range(8)]
    d10 = ((n[0] + n[2] + n[4] + n[6] + n[8]) * 7 - (n[1] + n[3] + n[5] + n[7])) % 10
    n.append(d10)
    n.append(sum(n) % 10)
    return "".join(map(str, n))


def make_iban(seed: int) -> str:
    rnd = random.Random(seed)
    bban = "".join(str(rnd.randint(0, 9)) for _ in range(5)) + "0" + \
           "".join(str(rnd.randint(0, 9)) for _ in range(16))
    check = 98 - int(bban + "292700") % 97
    return f"TR{check:02d}{bban}"


def make_card(prefix="4", length=16) -> str:
    body = prefix + "0" * (length - len(prefix) - 1)
    for d in range(10):
        if luhn_valid(body + str(d)):
            return body + str(d)
    raise AssertionError


def spaced(s: str, n: int = 4) -> str:
    return " ".join(s[i:i + n] for i in range(0, len(s), n))


# ---------------------------------------------------------------- validators

def test_validators():
    assert tckn_valid(VALID_TCKN) and tckn_valid(make_tckn(1))
    assert not tckn_valid("10000000147")       # wrong 11th digit
    assert not tckn_valid("01234567890")       # cannot start with 0
    assert iban_valid(make_iban(1)) and iban_valid("TR330006100519786457841326")
    assert not iban_valid("TR330006100519786457841327")
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


# --------------------------------------------------------------------- phones

@pytest.mark.parametrize("phone", [
    "0532 123 45 67",
    "05321234567",
    "+90 532 123 45 67",
    "+905321234567",
    "0090 532 123 45 67",
    "0 (532) 123-45-67",
    "(0532) 123 45 67",
    "532.123.45.67",
    "532 123 4567",
    "0212 555 12 34",                 # landline with prefix
    "+90 (212) 555 12 34",
])
def test_phone_formats(phone):
    r = mask_text(f"Numaram {phone}, not alır mısınız?")
    assert r.text == f"Numaram {PHONE}, not alır mısınız?"


def test_spoken_phone():
    r = mask_text("telefonum sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi")
    assert r.text == f"telefonum {PHONE}"


def test_phone_at_end_of_sentence_keeps_the_period():
    assert mask_text("Beni 0532 123 45 67. numaradan arayın").text == \
        f"Beni {PHONE}. numaradan arayın"


@pytest.mark.parametrize("text", [
    "Saat 15.30'da gelebilirim",
    "3 Kasım 2026 tarihinde",
    "2025 yılında 123 45 lira ödedim",
    "212 555 12 34",                  # landline without 0 / +90 is too ambiguous
    "Sipariş 5321234567890 numaralı",  # 13 digits: not a phone
])
def test_not_phones(text):
    assert mask_text(text).text == text


# ------------------------------------------------------------------- TCKN

def test_valid_tckn_is_masked_in_any_grouping():
    t = make_tckn(7)
    for form in (t, f"{t[:3]} {t[3:6]} {t[6:9]} {t[9:]}", "-".join([t[:5], t[5:]])):
        assert mask_text(f"Numaram {form} idi").text == f"Numaram {TCKN} idi"


def test_invalid_tckn_without_context_is_not_masked():
    # 11 digits, wrong checksum: could be an order number.
    assert mask_text("Sipariş numaram 12345678901").text == "Sipariş numaram 12345678901"


def test_invalid_tckn_with_explicit_context_is_still_masked():
    # One misheard digit must not leak the caller's real ID.
    r = mask_text("TC kimlik numaram 12345678901")
    assert r.text == f"TC kimlik numaram {TCKN}"
    assert r.spans[0].reason == "checksum_failed_context"


def test_spoken_tckn():
    r = mask_text("kimlik numaram on bir yüz on bir yüz on bir yüz on")  # 11 111 111 110
    assert TCKN in r.text or "11 111 111 110" in r.text  # checksum decides; no crash


# ------------------------------------------------------------------- IBAN

@pytest.mark.parametrize("fmt", [
    lambda i: i,
    lambda i: spaced(i),
    lambda i: i.lower(),
    lambda i: "-".join([i[:4], i[4:]]),
    lambda i: spaced(i[2:]),          # caller skipped the "TR"
])
def test_valid_iban_formats(fmt):
    iban = make_iban(3)
    assert mask_text(f"Para iadesi için {fmt(iban)} hesabıma").text == \
        f"Para iadesi için {IBAN} hesabıma"


def test_invalid_iban_without_context_is_not_masked():
    bad = "TR000006100519786457841326"
    assert mask_text(f"Referans kodu {bad} olarak geçiyor").text == \
        f"Referans kodu {bad} olarak geçiyor"


def test_invalid_iban_with_context_is_masked():
    r = mask_text("IBAN numaram TR000006100519786457841326")
    assert r.text == f"IBAN numaram {IBAN}"


# ------------------------------------------------------------------- cards

@pytest.mark.parametrize("form", [
    "4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
])
def test_card_luhn_valid(form):
    assert mask_text(f"Kart: {form} son kullanma 12/27").text == \
        f"Kart: {CARD} son kullanma 12/27"


def test_luhn_invalid_16_digits_without_context_not_masked():
    assert mask_text("Takip no 4111 1111 1111 1112").text == "Takip no 4111 1111 1111 1112"


def test_luhn_invalid_with_card_context_masked():
    assert mask_text("kredi kartı numaram 4111 1111 1111 1112").text == \
        f"kredi kartı numaram {CARD}"


# ------------------------------------------------------------------- e-mail

@pytest.mark.parametrize("email", [
    "ayse.yilmaz@example.com", "AYSE_Y+fatura@ornek.com.tr", "a.b-c@sub.domain.org",
])
def test_written_email(email):
    assert mask_text(f"Adresim {email}, teşekkürler").text == f"Adresim {EMAIL}, teşekkürler"


@pytest.mark.parametrize("spoken", [
    "ayse nokta yilmaz at gmail nokta com",
    "ahmet et hotmail nokta com nokta tr",
    "mehmet alt çizgi kaya at ornek nokta com",
])
def test_spoken_email(spoken):
    assert mask_text(f"mail adresim {spoken} olarak").text == f"mail adresim {EMAIL} olarak"


# Shapes whisper-large-v3 actually produced for spoken addresses in the real recordings
# (values here are synthetic): no "@", the dot already written as ".", "at"/"et" fused in.
@pytest.mark.parametrize("before, after", [
    ("ali.at.ornek.ai Evet.", f"{EMAIL} Evet."),                     # local.at.domain
    ("Yazdınız mı? ali.at.ai", f"Yazdınız mı? {EMAIL}"),
    ("Veli et ornek.ai", EMAIL),                                    # local et domain
    ("at gmail.com Evet doğrudur.", f"{EMAIL} Evet doğrudur."),     # local part lost
    ("Davet veli579.gmail.com adresine", f"Davet {EMAIL} adresine"),  # provider, no @
    ("579 Etci email.com Evet doğru.", f"579 {EMAIL} Evet doğru."),   # garbled local part
    ("E posta olsun. ali.kaya.com", f"E posta olsun. {EMAIL}"),     # 3+ labels
    ("aliveliornek.aiornek.ai olarak mı", f"{EMAIL} olarak mı"),
    ("e-posta adresinizi aliveli.ai olarak anladım",                 # 2 labels + context
     f"e-posta adresinizi {EMAIL} olarak anladım"),
    ("Mailim ali.com, yazar mısın", f"Mailim {EMAIL}, yazar mısın"),
])
def test_speech_to_text_email_shapes(before, after):
    r = mask_text(before)
    assert r.text == after
    assert r.counts["EMAIL"] == 1


@pytest.mark.parametrize("text", [
    "web sitemiz harbor.ai üzerinden bakabilirsiniz",   # a bare domain is not an address
    "adresimiz www.ornek.com.tr sayfasında",
    "kurumsal site ornek.gov.tr adresinde",
    "pazartesi saat 12.01'de görüşelim",
    "fiyatlandırılır. Net olarak söyleyemem",
    "Ahmet ata bak, at koşuyor",                       # Turkish "at" (horse) is not "@"
])
def test_domain_like_text_without_address_is_kept(text):
    assert mask_text(text).counts["EMAIL"] == 0


# -------------------------------------------------------------------- names

@pytest.mark.parametrize("text, expected", [
    ("Merhaba, adım Ayşe Yılmaz.", f"Merhaba, adım {NAME}."),
    ("ismim mehmet, size ulaşmak istedim", f"ismim {NAME}, size ulaşmak istedim"),
    ("Ben Zeynep, Harbor müşteri hizmetleri.", f"Ben {NAME}, Harbor müşteri hizmetleri."),
    ("Mehmet Bey, kaydınızı açtım.", f"{NAME} Bey, kaydınızı açtım."),
    ("Ahmet Yılmaz Bey ile görüştüm.", f"{NAME} Bey ile görüştüm."),
    ("Ayşe Hanım'ın talebi alındı.", f"{NAME} Hanım'ın talebi alındı."),
    ("Sayın Kemal Kaya, iyi günler.", f"Sayın {NAME}, iyi günler."),
    ("Dün Umut aradı ve bilgi verdi.", f"Dün {NAME} aradı ve bilgi verdi."),
    ("Selin'in siparişi gecikti.", f"{NAME}'in siparişi gecikti."),
])
def test_names(text, expected):
    assert mask_text(text).text == expected


@pytest.mark.parametrize("text", [
    "Umut ediyorum sorun çözülür.",          # sentence-initial ambiguous word
    "umut ediyorum sorun çözülür.",
    "Deniz kenarındaki şubeye gittim.",
    "Harbor'dan arıyorum, Kasım'da görüşelim.",
    "Barış içinde çözelim lütfen.",
    "Ben de aynı sorunu yaşadım.",
    "Pazartesi Ankara'ya gidiyorum.",
])
def test_not_names(text):
    assert mask_text(text).text == text


# ---------------------------------------------------------- complex sentences

def test_complex_turkish_sentence():
    tckn, iban = make_tckn(11), make_iban(11)
    text = (f"Merhaba, ben Ayşe Demir; TC kimlik numaram {tckn[:3]} {tckn[3:]}, "
            f"faturam iki kez çekildi, iadeyi {spaced(iban)} numaralı hesaba yapın. "
            f"Ulaşamazsanız sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi "
            f"veya ayse.demir@example.com adresine yazın. Perşembe saat on beşte "
            f"müsaitim.")
    r = mask_text(text)
    assert r.text == (
        f"Merhaba, ben {NAME}; TC kimlik numaram {TCKN}, faturam 2 kez çekildi, iadeyi "
        f"{IBAN} numaralı hesaba yapın. Ulaşamazsanız {PHONE} veya {EMAIL} adresine "
        f"yazın. Perşembe saat 15'te müsaitim."
    )
    assert dict(r.counts) == {"NAME": 1, "TCKN": 1, "IBAN": 1, "PHONE": 1, "EMAIL": 1}


def test_masking_is_idempotent():
    once = mask_text("Adım Ayşe, numaram 0532 123 45 67").text
    assert mask_text(once).text == once


def test_no_pii_text_is_unchanged_except_number_normalisation():
    assert mask_text("Faturam iki kez çekildi, yardımcı olur musunuz?").text == \
        "Faturam 2 kez çekildi, yardımcı olur musunuz?"


# ------------------------------------------- names missed in the real Groq run
# (values synthetic, shapes as seen in the real transcripts)

@pytest.mark.parametrize("before, after", [
    # an unlisted first name right before a surname + honorific
    ("Teşekkürler Yunus Akar Bey, kaydınızı aldım", f"Teşekkürler {NAME} Bey, kaydınızı aldım"),
    ("Anladım Selda Kurtuluş Hanım", f"Anladım {NAME} Hanım"),
    # common first names that were missing from the list
    ("Dün Yunus aradı", f"Dün {NAME} aradı"),
    ("Kerem ve Tuğçe geldi", f"{NAME} ve {NAME} geldi"),
])
def test_names_missed_in_real_run(before, after):
    assert mask_text(before).text == after


@pytest.mark.parametrize("text", [
    "Merhaba Ahmet Bey",          # greeting stays, the known name is masked
])
def test_greeting_before_honorific_is_not_a_name(text):
    assert mask_text(text).text == f"Merhaba {NAME} Bey"


ASK = {"speaker": "Temsilci", "channel": 0, "start": 0.0, "end": 1.0,
       "text": "Adınızı ve soyadınızı alabilir miyim?"}


def _answer(text):
    from app.steps import mask_transcript
    doc = {"segments": [ASK, {"speaker": "Müşteri", "channel": 1, "start": 1.0, "end": 2.0,
                              "text": text}]}
    return mask_transcript(doc)[0].splitlines()[1].removeprefix("Müşteri: ")


@pytest.mark.parametrize("answer, masked", [
    ("Çınar Özkul.", f"{NAME}."),                       # unlisted name and surname
    ("Evet, Doruk Erbil", f"Evet, {NAME}"),
    ("Doruk", NAME),
])
def test_answer_to_name_question_is_masked(answer, masked):
    assert _answer(answer) == masked


@pytest.mark.parametrize("answer", [
    "Online olsun.", "Tabii ki.", "Evet doğru.", "Bilmiyorum ki", "Neden soruyorsunuz?",
    "Toplantı Pazartesi olsun",
])
def test_ordinary_answer_after_name_question_is_kept(answer):
    assert _answer(answer) == answer


def test_capitalised_words_without_a_name_question_are_kept():
    from app.steps import mask_transcript
    doc = {"segments": [{"speaker": "Müşteri", "channel": 1, "start": 0.0, "end": 1.0,
                         "text": "Mavi Lojistik."}]}
    assert mask_transcript(doc)[0] == "Müşteri: Mavi Lojistik."


# Second real run: the three remaining shapes (values synthetic).
@pytest.mark.parametrize("answer", [
    "Çınar Kaplan Mavi Otomotiv",       # name + surname + company: four capitalised words
])
def test_long_answer_to_name_question(answer):
    out = _answer(answer)
    assert "Çınar" not in out and "Kaplan" not in out


def test_answer_with_suffix_keeps_the_suffix():
    assert _answer("Melda Özkul'um.") == f"{NAME}'um."


@pytest.mark.parametrize("before, after", [
    # distinctive surname at sentence start (company named after the owner)
    ("Özbek Otomotiv şirketimin adıyla kaydedin.",
     f"{NAME} Otomotiv şirketimin adıyla kaydedin."),
    # ambiguous first name at sentence start, right before a surname
    ("Evet. Çınar Kaplan geldi", f"Evet. {NAME} geldi"),
])
def test_remaining_real_name_shapes(before, after):
    assert mask_text(before).text == after


@pytest.mark.parametrize("text", [
    "Kaya gibi sağlam bir sistem",      # surname that is an everyday word: kept
    "Demir kapı kapalı",
    "Yıldız Turizm olarak arıyoruz",     # company named after a common word: kept
])
def test_everyday_word_surnames_at_sentence_start_are_kept(text):
    assert mask_text(text).text == text


# Third real run: the last two shapes (values synthetic).
@pytest.mark.parametrize("before, after", [
    ("Çınar.kaplan", NAME),                                   # name.surname handle
    ("Adres kerem.yurt olsun", f"Adres {NAME} olsun"),
    ("Melda Özkul'um.", f"{NAME}'um."),                   # "I am <name>" without a question
    ("Ben Doruk Erbil'im", f"Ben {NAME}'im"),
])
def test_handle_and_self_introduction(before, after):
    assert mask_text(before).text == after


@pytest.mark.parametrize("text", [
    "fatura.pdf dosyasını gönderdim",            # dotted word without a first name
    "Harbor'um var",                              # company + suffix is not a person
    "Ankara'yım şu an",
    "Pazartesi'yim",
])
def test_dotted_words_and_suffixes_that_are_not_names(text):
    assert mask_text(text).counts["NAME"] == 0


@pytest.mark.parametrize("answer, masked", [
    ("Çınar kaplan Yani ya bak", f"{NAME} Yani ya bak"),       # surname in lower case
    ("Çınar alacağım.", f"{NAME} alacağım."),                # surname garbled by STT
    ("Evet, Kerem yurt", f"Evet, {NAME}"),
])
def test_answer_starting_with_a_known_first_name(answer, masked):
    assert _answer(answer) == masked


def test_known_first_name_at_turn_start_without_question_is_unchanged():
    from app.steps import mask_transcript
    doc = {"segments": [{"speaker": "Müşteri", "channel": 1, "start": 0.0, "end": 1.0,
                         "text": "Umut ediyorum ki olur"}]}
    assert mask_transcript(doc)[0] == "Müşteri: Umut ediyorum ki olur"
