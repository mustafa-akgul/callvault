"""Turkish PII masking for call transcripts.

Pipeline: number words -> digits (numbers_tr), then detectors, then replacement.

  E-posta   written (a.b@x.com), spoken ("ahmet nokta yılmaz at gmail nokta com") and as
            speech-to-text writes it without "@" ("ali.at.ornek.ai", "ali579.gmail.com")
  IBAN      TR + 24 digits, ISO 13616 mod-97; also 24 digits without the spoken "TR"
  Kart      16 digits, Luhn
  TCKN      11 digits, official checksum (10th and 11th digit)
  Telefon   +90 / 0090 / 0 prefixes, mobile 5xx without prefix, landline with prefix
  İsim      context patterns ("adım ...", "... Hanım"), first-name / surname lists

Digits may be split into groups by spaces, dots, dashes or parentheses ("0532 123 45 67",
"(0532)-123-45-67"); detectors search over consecutive groups.

Checksums decide by default: an 11-digit number that is not a valid TCKN is left as is
(it may be an order number). One exception: when the caller announces the type right
before it ("TC kimlik numaram ...", "IBAN'ım ...", "kart numaram ..."), a number of the
right length is masked even if the checksum fails, because speech-to-text mishearing a
single digit must not leak the real identifier. These are reported as `*_context`.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.names_tr import AMBIGUOUS, FIRST_NAMES, NOT_NAMES, SURNAME_WORDS, SURNAMES
from app.numbers_tr import normalize_numbers, tr_lower

LABELS = {
    "NAME": "[İSİM MASKELENDİ]",
    "PHONE": "[TELEFON MASKELENDİ]",
    "TCKN": "[TCKN MASKELENDİ]",
    "IBAN": "[IBAN MASKELENDİ]",
    "CARD": "[KREDİ KARTI MASKELENDİ]",
    "EMAIL": "[E-POSTA MASKELENDİ]",
}
# Higher wins when spans overlap.
PRIORITY = {"EMAIL": 6, "IBAN": 5, "CARD": 4, "TCKN": 3, "PHONE": 2, "NAME": 1}

CONTEXT_WINDOW = 60
CONTEXT = {
    "TCKN": ("kimlik", "tc ", "t.c", "tckn", "tc'", "tc no", "tc numara"),
    "IBAN": ("iban",),
    "CARD": ("kart",),
}


# ------------------------------------------------------------------ validators

def tckn_valid(d: str) -> bool:
    if len(d) != 11 or not d.isdigit() or d[0] == "0":
        return False
    n = [int(c) for c in d]
    d10 = ((n[0] + n[2] + n[4] + n[6] + n[8]) * 7 - (n[1] + n[3] + n[5] + n[7])) % 10
    d11 = sum(n[:10]) % 10
    return n[9] == d10 and n[10] == d11


def iban_valid(iban: str) -> bool:
    s = re.sub(r"[\s-]", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    if s.startswith("TR") and len(s) != 26:
        return False
    rearranged = s[4:] + s[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


def luhn_valid(d: str) -> bool:
    if not d.isdigit() or len(d) < 12:
        return False
    total = 0
    for i, c in enumerate(reversed(d)):
        x = int(c)
        if i % 2 == 1:
            x *= 2
            if x > 9:
                x -= 9
        total += x
    return total % 10 == 0


def phone_national(d: str, plus: bool) -> str | None:
    """National 10-digit number if `d` is a Turkish phone number, else None."""
    prefixed = False
    if d.startswith("0090"):
        d, prefixed = d[4:], True
    elif d.startswith("90") and (plus or len(d) == 12):
        d, prefixed = d[2:], True
    if len(d) == 11 and d.startswith("0"):
        d, prefixed = d[1:], True
    if len(d) != 10:
        return None
    if d[0] == "5":
        return d
    if d[0] in "234" and prefixed:
        return d
    return None


# ----------------------------------------------------------------------- spans

@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str
    reason: str = "pattern"


@dataclass
class MaskResult:
    text: str
    counts: Counter = field(default_factory=Counter)
    spans: list[Span] = field(default_factory=list)


def _has_context(text: str, start: int, kind: str) -> bool:
    raw = text[max(0, start - CONTEXT_WINDOW): start]
    # Both lowerings: Turkish turns "IBAN" into "ıban", ASCII keeps "iban".
    windows = (tr_lower(raw), raw.lower())
    return any(k in w for w in windows for k in CONTEXT.get(kind, ()))


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
_SPOKEN_EMAIL = re.compile(
    r"\b[\w]+(?:\s+(?:nokta|alt çizgi|tire)\s+[\w]+)*\s+(?:at|et)\s+[\w]+"
    r"(?:\s+nokta\s+[\w]+)*\s+nokta\s+(?:com|net|org|edu|gov|info|io|tr)"
    r"(?:\s+nokta\s+tr)?\b",
    re.UNICODE | re.IGNORECASE,
)
_IBAN_TR = re.compile(r"(?<![\w])TR[ \t-]?\d{2}(?:[ \t-]?\d){22}(?!\d)", re.IGNORECASE)
_DIGIT_GROUP = re.compile(r"\+?\d+")
_GROUP_SEP = re.compile(r"[ \t.\-/()]{1,3}")


# What speech-to-text really writes for a spoken address (seen with whisper-large-v3 on the
# real calls): no "@", the dots already written as ".", "at"/"et" kept as a word or fused
# between dots ("ali.at.ornek.ai", "Veli et ornek.ai", "veli579.gmail.com").
_TLD = r"(?:com|net|org|edu|gov|info|io|ai|co|biz|me|tr)"
_END = r"(?!\w)(?!\.\w)"
_LEAD = r"(?<![\w.@/-])"
_STT_EMAIL = [
    # local.at.domain[.tld]
    (re.compile(_LEAD + r"[\w+-]+(?:\.[\w+-]+)*\.(?:at|et)\.[\w-]+(?:\.[\w-]+)*" + _END,
                re.IGNORECASE), "stt_at_dotted"),
    # [local ]at domain.tld
    (re.compile(_LEAD + r"(?:[\w+-]+(?:\.[\w+-]+)*\s+)?(?:at|et)\s+[\w-]+(?:\.[\w-]+)*\."
                + _TLD + r"(?:\.tr)?" + _END, re.IGNORECASE), "stt_at_spaced"),
    # local.gmail.com: a mail provider can only mean an address
    (re.compile(_LEAD + r"[\w.+-]+[.\s](?:gmail|googlemail|hotmail|outlook|yahoo|yandex|"
                r"icloud|live|msn|e?mail)\.com(?:\.tr)?" + _END, re.IGNORECASE), "stt_provider"),
]
_DOMAIN = re.compile(_LEAD + r"(?!www\.)[\w+-]+(?:\.[\w+-]+)*\." + _TLD + r"(?:\.tr)?" + _END,
                     re.IGNORECASE)
_EMAIL_CONTEXT = ("mail", "e-posta", "e posta", "eposta")
_TLD_WORDS = {"com", "net", "org", "edu", "gov", "info", "io", "ai", "co", "biz", "me", "tr",
              "gen", "web", "bel", "k12", "av", "pol", "tsk", "name"}


def _name_labels(host: str) -> list[str]:
    """Labels left after removing the public suffix: 'ali.kaya.com' -> ['ali', 'kaya'],
    'ornek.gov.tr' -> ['ornek']."""
    labels = host.lower().split(".")
    while labels and labels[-1] in _TLD_WORDS:
        labels.pop()
    return labels


def find_emails(text: str) -> list[Span]:
    spans = [Span(m.start(), m.end(), "EMAIL") for m in _EMAIL.finditer(text)]
    spans += [Span(m.start(), m.end(), "EMAIL", "spoken") for m in _SPOKEN_EMAIL.finditer(text)]
    for rx, reason in _STT_EMAIL:
        spans += [Span(m.start(), m.end(), "EMAIL", reason) for m in rx.finditer(text)]
    for m in _DOMAIN.finditer(text):
        # A bare "harbor.ai" is a website. "ali.kaya.com" (two name labels before the
        # suffix) is how an address sounds once "@" is lost; so is any domain right after
        # the caller talks about e-mail.
        if len(_name_labels(m.group())) >= 2:
            spans.append(Span(m.start(), m.end(), "EMAIL", "stt_multilabel"))
        else:
            window = text[max(0, m.start() - CONTEXT_WINDOW): m.start()]
            if any(k in w for w in (tr_lower(window), window.lower()) for k in _EMAIL_CONTEXT):
                spans.append(Span(m.start(), m.end(), "EMAIL", "stt_domain_context"))
    return spans


def find_tr_ibans(text: str) -> list[Span]:
    spans = []
    for m in _IBAN_TR.finditer(text):
        if iban_valid(m.group()):
            spans.append(Span(m.start(), m.end(), "IBAN"))
        elif _has_context(text, m.start(), "IBAN"):
            spans.append(Span(m.start(), m.end(), "IBAN", "checksum_failed_context"))
    return spans


def _digit_runs(text: str) -> list[list[re.Match]]:
    """Consecutive digit groups separated only by short separators."""
    runs: list[list[re.Match]] = []
    for m in _DIGIT_GROUP.finditer(text):
        if m.start() > 0 and (text[m.start() - 1].isalnum() or text[m.start() - 1] == "_"):
            continue
        if m.end() < len(text) and (text[m.end()].isalpha() or text[m.end()] == "_"):
            continue
        if runs:
            prev = runs[-1][-1]
            if _GROUP_SEP.fullmatch(text[prev.end(): m.start()]) and "+" not in m.group():
                runs[-1].append(m)
                continue
        runs.append([m])
    return runs


def _classify(text: str, start: int, digits: str, plus: bool) -> Span | None:
    n = len(digits)
    if n == 24 and iban_valid("TR" + digits):
        return Span(start, -1, "IBAN", "no_country_code")
    if n == 16:
        if luhn_valid(digits):
            return Span(start, -1, "CARD")
        if _has_context(text, start, "CARD"):
            return Span(start, -1, "CARD", "checksum_failed_context")
    if n == 11 and digits[0] != "0":
        if tckn_valid(digits):
            return Span(start, -1, "TCKN")
        if _has_context(text, start, "TCKN"):
            return Span(start, -1, "TCKN", "checksum_failed_context")
    if phone_national(digits, plus):
        return Span(start, -1, "PHONE")
    return None


def find_digit_entities(text: str) -> list[Span]:
    spans: list[Span] = []
    for run in _digit_runs(text):
        i = 0
        while i < len(run):
            found = None
            # Longest match first, starting at group i.
            for j in range(len(run) - 1, i - 1, -1):
                digits = "".join(g.group().lstrip("+") for g in run[i: j + 1])
                if len(digits) > 26:
                    continue
                span = _classify(text, run[i].start(), digits, run[i].group().startswith("+"))
                if span:
                    start = span.start
                    if start > 0 and text[start - 1] == "(":   # "(0532) 123 45 67"
                        start -= 1
                    found = (Span(start, run[j].end(), span.kind, span.reason), j)
                    break
            if found:
                spans.append(found[0])
                i = found[1] + 1
            else:
                i += 1
    return spans


# ------------------------------------------------------------------------ names

_CAP = r"[A-ZÇĞİÖŞÜ][a-zçğıöşü]+"
_TOKEN = re.compile(_CAP)
# Case-sensitive on purpose: the optional surname must be capitalised.
_INTRO = re.compile(
    r"\b(?:[Aa]dım(?:\s+soyadım)?|[İi]smim(?:\s+soyismim)?|[Ss]oyadım|[Aa]d\s+soyad)"
    r"(?:\s+de|\s+da)?[:,]?\s+"
    r"([A-Za-zÇĞİÖŞÜçğıöşü]+(?:\s+" + _CAP + r")?)"
)
_BEN = re.compile(r"\b[Bb]en\s+(" + _CAP + r")(?:\s+(" + _CAP + r"))?")
_HONORIFIC = re.compile(r"(?:(" + _CAP + r")\s+)?(" + _CAP + r")\s+(?:Bey|Hanım|Bay|Bayan)\b")
_SAYIN = re.compile(r"\b[Ss]ayın\s+(" + _CAP + r")(?:\s+(" + _CAP + r"))?")


def _cap_word(word: str) -> str:
    return word[:1].replace("i", "İ").upper() + word[1:]


def _is_sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip(" \t\"'")
    return not before or before[-1] in ".!?:\n"


def _is_name_word(word: str) -> bool:
    return word not in NOT_NAMES and (word in FIRST_NAMES or word in SURNAMES)


# A turn that answers "Adınızı alabilir miyim?" with nothing but 1-3 capitalised words
# ("Çınar Özkul.", "Evet, Doruk Erbil") is a name, whether or not it is in the list.
NAME_QUESTION = re.compile(
    r"\b(?:ad[ıi]n[ıi]z|ad[ıi]n[ıi]z[ıi]|soyad|isminiz|isminizi|ismini|ad[ıi]n[ıi]|"
    r"kiminle\s+görüşüyorum)", re.IGNORECASE)
_ANSWER = re.compile(
    r"^\s*(?:(?:Evet|Tabii|Tabi|Tamam|Ben|Benim|Adım|İsmim|Efendim)[,.]?\s+)?"
    r"(" + _CAP + r"(?:\s+" + _CAP + r"){0,3})(?:'[a-zçğıöşü]+)?\s*[.!]?\s*$")


_ANSWER_START = re.compile(
    r"^\s*(?:(?:Evet|Tabii|Tabi|Tamam|Ben|Benim|Adım|İsmim|Efendim)[,.]?\s+)?"
    r"([A-Za-zÇĞİÖŞÜçğıöşü]+)(?:\s+([A-Za-zÇĞİÖŞÜçğıöşü]+))?")

# "Çınar.kaplan": a name typed like an e-mail handle (seen in the real run).
_HANDLE = re.compile(r"(?<![\w.@])([A-Za-zÇĞİÖŞÜçğıöşü]+)\.([a-zçğıöşü]+)(?![\w.@])")
# "Melda Özkul'um" = "I am Yıldız Özkul": copula "-(y)Im" on a capitalised name.
_I_AM = re.compile(r"(" + _CAP + r"(?:\s+" + _CAP + r")?)'(?:y?[ıiuü]m)\b")


def find_names(text: str, asked_name: bool = False) -> list[Span]:
    spans: list[Span] = []

    for m in _HANDLE.finditer(text):
        if _cap_word(tr_lower(m.group(1))) in FIRST_NAMES:
            spans.append(Span(m.start(), m.end(), "NAME", "handle"))

    for m in _I_AM.finditer(text):
        words = m.group(1).split()
        if not any(w in NOT_NAMES for w in words):
            spans.append(Span(m.start(1), m.end(1), "NAME", "self_introduction"))

    if asked_name:
        m = _ANSWER.match(text)
        if m and not any(w in NOT_NAMES for w in m.group(1).split()):
            spans.append(Span(m.start(1), m.end(1), "NAME", "answer_to_name_question"))
        # STT often writes the surname in lower case or garbles it ("Çınar kaplan",
        # "Çınar alacağım"): a known first name opening the answer is the name, and a
        # following word that is a known surname goes with it.
        m = _ANSWER_START.match(text)
        if m and _cap_word(m.group(1)) in FIRST_NAMES and _cap_word(m.group(1)) not in NOT_NAMES:
            end = m.end(1)
            if m.group(2) and _cap_word(tr_lower(m.group(2))) in SURNAMES:
                end = m.end(2)
            spans.append(Span(m.start(1), end, "NAME", "answer_to_name_question"))

    for m in _INTRO.finditer(text):
        words = m.group(1).split()
        if _cap_word(words[0]) in NOT_NAMES:
            continue
        # Lower-case after "adım" only if it is a known first name.
        if words[0][0].islower() and _cap_word(words[0]) not in FIRST_NAMES:
            continue
        spans.append(Span(m.start(1), m.end(1), "NAME", "introduction"))

    for m in _BEN.finditer(text):
        first, second = m.group(1), m.group(2)
        if first in FIRST_NAMES and first not in NOT_NAMES:
            end = m.end(2) if second and second not in NOT_NAMES else m.end(1)
            spans.append(Span(m.start(1), end, "NAME", "introduction"))

    for m in _HONORIFIC.finditer(text):
        start = m.start(2)
        # "Yunus Akar Bey": a capitalised word mid-sentence before "<surname> Bey" is the
        # first name even when it is not in the list (the real calls had such names).
        if m.group(1) and m.group(1) not in NOT_NAMES and (
                _is_name_word(m.group(1)) or not _is_sentence_start(text, m.start(1))):
            start = m.start(1)
        if m.group(2) not in NOT_NAMES:
            spans.append(Span(start, m.end(2), "NAME", "honorific"))

    for m in _SAYIN.finditer(text):
        end = m.end(2) if m.group(2) and m.group(2) not in NOT_NAMES else m.end(1)
        spans.append(Span(m.start(1), end, "NAME", "honorific"))

    # Dictionary: capitalised known first names, plus a following capitalised surname.
    tokens = list(_TOKEN.finditer(text))
    for k, m in enumerate(tokens):
        word = m.group()
        if word in NOT_NAMES or word not in FIRST_NAMES and word not in SURNAMES:
            continue
        ambiguous = word in AMBIGUOUS or (word in SURNAME_WORDS and word not in FIRST_NAMES)
        if ambiguous and _is_sentence_start(text, m.start()):
            continue
        end = m.end()
        if k + 1 < len(tokens):
            nxt = tokens[k + 1]
            if (text[m.end(): nxt.start()].isspace() and nxt.group() not in NOT_NAMES
                    and (nxt.group() in SURNAMES or word in FIRST_NAMES)):
                end = nxt.end()
        spans.append(Span(m.start(), end, "NAME", "dictionary"))

    # A first name right before a detected name is part of it, even when it was skipped
    # as ambiguous ("Çınar Kaplan" at the start of a sentence: "Çınar" is also a tree).
    for k, m in enumerate(tokens):
        word = m.group()
        if word in NOT_NAMES or word not in FIRST_NAMES or k + 1 >= len(tokens):
            continue
        nxt = tokens[k + 1]
        if not text[m.end(): nxt.start()].isspace():
            continue
        following = [s for s in spans if s.start == nxt.start()]
        if following:
            spans.append(Span(m.start(), max(s.end for s in following), "NAME", "adjacent"))
    return spans


# ----------------------------------------------------------------------- masking

def _resolve(spans: list[Span]) -> list[Span]:
    """Keep non-overlapping spans, preferring higher priority, then longer."""
    chosen: list[Span] = []
    for s in sorted(spans, key=lambda s: (-PRIORITY[s.kind], -(s.end - s.start), s.start)):
        if all(s.end <= c.start or s.start >= c.end for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def mask_text(text: str, asked_name: bool = False) -> MaskResult:
    """`asked_name`: the previous turn (other speaker) asked for the caller's name."""
    normalized = normalize_numbers(text)
    spans = _resolve(
        find_emails(normalized)
        + find_tr_ibans(normalized)
        + find_digit_entities(normalized)
        + find_names(normalized, asked_name)
    )
    out, pos = [], 0
    for s in spans:
        out.append(normalized[pos: s.start])
        out.append(LABELS[s.kind])
        pos = s.end
    out.append(normalized[pos:])
    return MaskResult("".join(out), Counter(s.kind for s in spans), spans)
