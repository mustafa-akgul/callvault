import pytest

from app.numbers_tr import normalize_numbers, words_to_groups


@pytest.mark.parametrize("text, expected", [
    ("beş yüz otuz iki", "532"),
    ("yüz bin", "100000"),
    ("iki bin yirmi beş", "2025"),
    ("bin dokuz yüz doksan dokuz", "1999"),
    ("iki milyon beş yüz bin", "2500000"),
    ("on bir", "11"),
    ("yüz bir", "101"),
    # Digit dictation: a new group starts when the number cannot continue.
    ("sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi", "0 532 123 45 67"),
    ("beş üç iki", "5 3 2"),
    ("sıfır sıfır", "0 0"),
    # Mixed digits and words.
    ("numaram 0532 yüz yirmi üç kırk beş altmış yedi", "numaram 0532 123 45 67"),
    # Suffix on the last word is kept.
    ("saat on beşte", "saat 15'te"),
    ("dörde kadar", "4'e kadar"),
    ("üçüncü kat", "3'üncü kat"),
    # Capitalised / Turkish dotted I.
    ("İki Yüz", "200"),
    ("ALTI", "6"),
])
def test_normalize(text, expected):
    assert normalize_numbers(text) == expected


@pytest.mark.parametrize("text", [
    "bir sorun var",          # article, not 1
    "biri aradı",             # "someone"
    "altında kaldı",          # not a number word + known suffix
    "Kasım ayında",
    "yüzde",                  # not converted? -> see below
])
def test_leaves_non_numbers_alone(text):
    if text == "yüzde":
        # "yüzde" (percent) = yüz + de; converting it is acceptable but must not crash.
        assert normalize_numbers(text) in ("yüzde", "100'de")
    else:
        assert normalize_numbers(text) == text


def test_punctuation_ends_a_number():
    assert normalize_numbers("beş, altı") == "5, 6"


def test_groups_api():
    assert words_to_groups(["sıfır", "beş", "yüz", "otuz", "iki"]) == [0, 532]


# "yüz" is also "face": idioms seen in the real calls must not become numbers.
@pytest.mark.parametrize("text", [
    "ofisimizde yüz yüze görüşelim",
    "Yüz yüze tanışabiliyor muyuz?",
    "yüzyüze olsun",
    "online değil, yüz yüze mi tercih edersiniz",
    "bu yüzden aradım",
    "o yüzden olmadı",
    "şu yüzden soruyorum",
    "onun yüzünden",
])
def test_face_idioms_are_not_numbers(text):
    assert normalize_numbers(text) == text


@pytest.mark.parametrize("text, expected", [
    ("yüz yirmi üç", "123"),              # the number still works
    ("yüz yüz elli", "100 150"),          # dictation: "yüz" twice is still numbers
    ("yüze kadar say", "100'e kadar say"),
    ("yüz yüze ve yüz elli", "yüz yüze ve 150"),
])
def test_hundred_still_parsed_next_to_the_idiom(text, expected):
    assert normalize_numbers(text) == expected


# "on" is also the stem of the pronoun "o" (onu, ona, onun, onda, ondan): the real calls
# had "onu" -> "10'u" and "ona" -> "10'a". A lone pronoun form stays a word...
@pytest.mark.parametrize("text", [
    "onu aradım", "ona söyledim", "onun yüzünden", "ondan haber yok", "onda kaldı",
    "Onu da etkiliyor mu?",
])
def test_pronoun_o_is_not_ten(text):
    assert normalize_numbers(text) == text


# ...but it is a number after "saat", or inside a longer number.
@pytest.mark.parametrize("text, expected", [
    ("saat onda görüşelim", "saat 10'da görüşelim"),
    ("saat ondan sonra", "saat 10'dan sonra"),
    ("yüz ona kadar", "110'a kadar"),
    ("on", "10"),
])
def test_ten_with_suffix_still_parsed_in_number_context(text, expected):
    assert normalize_numbers(text) == expected


# STT often writes the digits and the scale word separately: "10 bin dakika".
@pytest.mark.parametrize("text, expected", [
    ("ayda 10 bin dakika", "ayda 10000 dakika"),
    ("on bin dakika", "10000 dakika"),
    ("2 bin 500 lira", "2500 lira"),
    ("3 milyon", "3000000"),
    ("1.5 bin", "1.5 1000"),          # decimals are not guessed
    ("0532 bin", "0532 1000"),        # a leading zero is a dictated group, not a quantity
])
def test_digits_followed_by_a_scale_word(text, expected):
    assert normalize_numbers(text) == expected
