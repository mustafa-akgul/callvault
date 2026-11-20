"""Turkish number words -> digits, as a pre-processing step for PII detection.

Callers dictate numbers in groups ("sıfır beş yüz otuz iki, yüz yirmi üç, kırk beş,
altmış yedi"), and speech-to-text writes them as words, digits or a mix. PII patterns
work on digits, so number words are turned into digit groups first:

    "sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi" -> "0 532 123 45 67"
    "yüz bin"                                                    -> "100000"
    "iki bin yirmi beş"                                          -> "2025"
    "saat on beşte"                                              -> "saat 15'te"

A new group starts whenever the next word cannot continue the current number
("otuz iki" + "kırk" -> 32, 40...), which is exactly how digit dictation sounds.
A lone "bir" is left alone: it is far more often the article ("bir sorun") than 1.
"""

from __future__ import annotations

import re

UNITS = {"sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6,
         "yedi": 7, "sekiz": 8, "dokuz": 9}
TENS = {"on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60,
        "yetmiş": 70, "seksen": 80, "doksan": 90}
SCALES = {"bin": 1_000, "milyon": 1_000_000, "milyar": 1_000_000_000}
HUNDRED = "yüz"

# Stems that only appear with a suffix (consonant softening: dört -> dörde).
SOFTENED = {"dörd": "dört"}

# Case / possessive / ordinal / copula suffixes that may follow the LAST number word.
SUFFIXES = sorted({
    "e", "a", "ye", "ya", "de", "da", "te", "ta", "den", "dan", "ten", "tan",
    "i", "ı", "u", "ü", "yi", "yı", "yu", "yü", "in", "ın", "un", "ün",
    "nin", "nın", "nun", "nün", "le", "la", "yle", "yla", "li", "lı", "lu", "lü",
    "dir", "dır", "dur", "dür", "tir", "tır", "tur", "tür", "lik", "lık", "luk", "lük",
    "inci", "ıncı", "uncu", "üncü", "nci", "ncı", "ncu", "ncü", "er", "ar", "şer", "şar",
}, key=len, reverse=True)

ALL_WORDS = {**UNITS, **TENS, **SCALES, HUNDRED: 100}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# "yüz" is also "face". These idioms appeared in the real calls and were turned into
# "100 100'e" / "bu 100'den"; their words are never treated as numbers.
_IDIOM = re.compile(r"\byüz\s+yüze\b|\b(?:bu|o|şu|bunun|onun|şunun|ne)\s+yüzden\b")

# Case forms of the pronoun "o" that look like "on" (ten) + suffix. Alone, they are read as
# the pronoun, unless the previous word is "saat" ("saat onda" = at ten).
PRONOUN_O_SUFFIXES = {"u", "a", "un", "da", "dan", "la"}

# A quantity in digits right before a scale word: 1-999, no leading zero (that would be a
# dictated digit group, e.g. "0532"), not part of a decimal ("1.5 bin" is not guessed).
_LEAD_DIGITS = re.compile(r"(?<![\d.,])([1-9]\d{0,2})\s+$")
_TAIL_DIGITS = re.compile(r"\s+([1-9]\d{0,2})(?![\d.,])")


def _after_saat(text: str, matches: list[re.Match], i: int) -> bool:
    return i > 0 and tr_lower(matches[i - 1].group()) == "saat"


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def _split_word(word: str) -> tuple[str, str] | None:
    """('yirmi', '') for 'yirmi', ('beş', 'te') for 'beşte', None if not a number."""
    w = tr_lower(word)
    if w in ALL_WORDS:
        return w, ""
    for suffix in SUFFIXES:
        if w.endswith(suffix) and len(w) > len(suffix):
            stem = w[: -len(suffix)]
            stem = SOFTENED.get(stem, stem)
            if stem in ALL_WORDS:
                return stem, suffix
    return None


def words_to_groups(words: list[str]) -> list[int]:
    """Parse number words into the digit groups a listener would write down."""
    groups: list[int] = []
    total = cur = 0
    last: str | None = None
    started = False

    def flush() -> None:
        nonlocal total, cur, last, started
        if started:
            groups.append(total + cur)
        total = cur = 0
        last = None
        started = False

    for w in words:
        if w == "sıfır":
            flush()
            groups.append(0)
            continue
        if w in UNITS:
            if started and (cur % 10 != 0 or last == "unit"):
                flush()
            cur += UNITS[w]
            last = "unit"
        elif w in TENS:
            if started and (cur % 100 != 0 or last in ("unit", "ten")):
                flush()
            cur += TENS[w]
            last = "ten"
        elif w == HUNDRED:
            if started and last == "unit" and cur < 10:
                cur *= 100                       # "beş yüz"
            elif started and cur == 0 and last == "scale":
                cur = 100                        # "bin yüz"
            else:
                if started:
                    flush()
                cur = 100
            last = "hundred"
        else:  # bin / milyon / milyar
            v = SCALES[w]
            if not started:
                total, cur = v, 0                # "bin" alone = 1000
            elif cur == 0 or (total and (total % (v * 1000) != 0)):
                flush()                          # "bin bin", "bin beş bin"
                total = v
            else:
                total += cur * v
                cur = 0
            last = "scale"
        started = True
    flush()
    return groups


def normalize_numbers(text: str) -> str:
    """Replace runs of Turkish number words with digit groups. Other text is untouched."""
    out: list[str] = []
    pos = 0
    matches = list(_WORD.finditer(text))
    # tr_lower keeps string length, so idiom offsets line up with `text`.
    idioms = [(m.start(), m.end()) for m in _IDIOM.finditer(tr_lower(text))]

    def in_idiom(m: re.Match) -> bool:
        return any(a <= m.start() < b for a, b in idioms)

    i = 0
    while i < len(matches):
        parsed = None if in_idiom(matches[i]) else _split_word(matches[i].group())
        if parsed is None:
            i += 1
            continue
        # Extend the run over whitespace-separated number words; a suffix ends it.
        run = [parsed]
        j = i
        while not run[-1][1] and j + 1 < len(matches):
            gap = text[matches[j].end(): matches[j + 1].start()]
            nxt = None if in_idiom(matches[j + 1]) else _split_word(matches[j + 1].group())
            if nxt is None or not gap.isspace():
                break
            run.append(nxt)
            j += 1
        stems = [s for s, _ in run]
        if stems == ["bir"]:          # article, not a number
            i = j + 1
            continue
        if stems == ["on"] and run[0][1] in PRONOUN_O_SUFFIXES and not _after_saat(text, matches, i):
            i = j + 1                 # onu / ona / onun / onda / ondan: the pronoun "o"
            continue
        suffix = run[-1][1]
        pre = text[pos: matches[i].start()]
        end = matches[j].end()
        # "10 bin" / "2 bin 500": STT wrote the quantity in digits and the scale in words.
        lead = _LEAD_DIGITS.search(pre) if stems in (["bin"], ["milyon"], ["milyar"]) else None
        if lead:
            scale = SCALES[stems[0]]
            value = int(lead.group(1)) * scale
            tail = None if suffix else _TAIL_DIGITS.match(text, end)
            if tail and int(tail.group(1)) < scale:
                value += int(tail.group(1))
                end = tail.end()
            pre = pre[: lead.start()]
            rendered = str(value)
        else:
            rendered = " ".join(str(g) for g in words_to_groups(stems))
        if suffix:
            rendered += "'" + suffix
        out.append(pre)
        out.append(rendered)
        pos = end
        i = j + 1
        while i < len(matches) and matches[i].start() < pos:
            i += 1
    out.append(text[pos:])
    return "".join(out)
