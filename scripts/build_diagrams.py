"""Build the README diagrams as plain SVG (no dependencies).

    python scripts/build_diagrams.py        # writes docs/img/*.svg

Every figure explains one idea of the system:
  hero.svg          the guarantee, shown as the result of a real chaos run
  architecture.svg  three layers, one database, every arrow a transaction
  exactly-once.svg  why a lost response never becomes a duplicate booking
  tenant-model.svg  how a query without tenant context fails instead of leaking
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"

PAPER, CARD, LINE = "#FAF8F5", "#FFFFFF", "#DDD6CA"
INK, MUTED = "#1F2328", "#5F6670"
DATA, OK, CHAOS = "#3B5B92", "#2F7D4F", "#C2410C"
SANS = "'Segoe UI', Inter, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, 'Cascadia Mono', Consolas, Menlo, monospace"
K = 1.3            # body text (<= 17 px) is scaled up: GitHub shows these at ~72%
TEST_COUNT = 387   # last full run: `pytest -q` on the sample dataset, 0 skipped


class Svg:
    def __init__(self, w: int, h: int, title: str, desc: str):
        self.w, self.h, self.parts = w, h, []
        markers = "".join(
            f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" '
            f'fill="{color}"/></marker>'
            for mid, color in (("a", MUTED), ("ac", CHAOS), ("ao", OK)))
        self.head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
                     f'width="{w}" height="{h}" role="img" aria-labelledby="t d">'
                     f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>'
                     f'<defs>{markers}</defs><rect width="{w}" height="{h}" fill="{PAPER}"/>')

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, size=14, fill=INK, weight=400, anchor="start", mono=False):
        fam = MONO if mono else SANS
        size = round(size * K, 1) if size <= 17 else size
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')

    def rect(self, x, y, w, h, fill=CARD, stroke=LINE, r=10, dash=None, sw=1.2):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, color=MUTED, marker="a", dash=None, sw=1.6):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if marker else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                 f'stroke-width="{sw}"{d}{m}/>')

    def path(self, d, color=MUTED, marker="a", dash=None, sw=1.6):
        dd = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if marker else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{dd}{m}/>')

    def card(self, x, y, w, h, num, title, lines, accent=DATA):
        self.rect(x, y, w, h)
        self.add(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>')
        self.text(x + 18, y + 26, num, 12, accent, 700, mono=True)
        self.text(x + 18, y + 50, title, 15, INK, 650)
        for i, ln in enumerate(lines):
            self.text(x + 18, y + 76 + i * 22, ln, 12.5, MUTED)

    def save(self, name: str) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(self.head + "".join(self.parts) + "</svg>\n", encoding="utf-8")
        print("wrote", OUT / name)


# ------------------------------------------------------------------- hero

def hero() -> None:
    s = Svg(1280, 420, "Callvault: no call lost, no booking doubled",
            "A call-recording pipeline that survives killed workers and a failing CRM.")
    s.text(72, 96, "CALLVAULT", 17, CHAOS, 700)
    s.text(72, 170, "No call lost.", 58, INK, 750)
    s.text(72, 232, "No booking doubled.", 58, INK, 750)
    s.text(72, 274, "Call recordings in. Masked data and CRM actions out.", 19, MUTED)
    for i, (k, v, c) in enumerate([(str(TEST_COUNT), "tests", DATA), ("0", "calls lost", OK),
                                   ("0", "duplicates", OK)]):
        x = 72 + i * 170
        s.text(x, 346, k, 44, c, 750)
        s.text(x, 376, v, 13, MUTED)

    x0, y0, w, h = 700, 48, 508, 324
    s.rect(x0, y0, w, h, fill="#1B1F24", stroke="#1B1F24", r=14)
    for i, c in enumerate(["#E5534B", "#D4A72C", "#57AB5A"]):
        s.add(f'<circle cx="{x0 + 24 + i * 20}" cy="{y0 + 24}" r="6" fill="{c}"/>')
    s.text(x0 + 100, y0 + 30, "python -m app.cli demo-chaos", 12.5, "#8B949E", mono=True)
    rows = [("OK", "started 3 worker processes", "#57AB5A", "#C9D1D9"),
            ("KILL", "SIGKILL worker mid-job", "#E5534B", "#C9D1D9"),
            ("OK", "replacement worker starts", "#57AB5A", "#C9D1D9"),
            ("", "lease expires, job reclaimed", "#8B949E", "#8B949E"),
            ("", "CRM: 429, 500, stalls, drops", "#8B949E", "#8B949E")]
    for i, (tag, msg, tc, mc) in enumerate(rows):
        y = y0 + 74 + i * 32
        if tag:
            s.text(x0 + 24, y, tag, 13, tc, 700, mono=True)
        s.text(x0 + 84, y, msg, 13, mc, mono=True)
    s.rect(x0 + 20, y0 + 232, w - 40, 76, fill="#14281D", stroke="#2F7D4F", r=8)
    s.text(x0 + 38, y0 + 264, "verdict: 0 lost, 0 duplicates", 16, "#7EE2A0", 700, mono=True)
    s.text(x0 + 38, y0 + 292, "4/4 bookings, 4/4 complaints", 12.5, "#9DD9B3", mono=True)
    s.save("hero.svg")


# ----------------------------------------------------------- architecture

def architecture() -> None:
    s = Svg(1280, 860, "Callvault architecture",
            "Ingestion, pipeline and CRM delivery layers on one PostgreSQL database with "
            "row-level security.")
    s.text(48, 56, "Architecture", 26, INK, 750)
    s.text(48, 82, "Every arrow between steps is one database transaction: output and next state "
                   "are written together.", 14, MUTED)

    cw, ch, gap = 280, 128, 40
    layers = [
        ("1  Ingestion", 112, [
            ("STEP 0", "Manifest", ["recordings + tenant", "idempotent re-ingest"]),
            ("STEP 1", "Deduplicate", ["SHA-256 of the audio", "byte copy becomes DUPLICATE"]),
            ("STEP 2", "Validate WAV", ["header vs. length, silence", "bad file fails with a reason"]),
        ], DATA),
        ("2  Pipeline", 300, [
            ("QUEUE", "Job queue", ["FOR UPDATE SKIP LOCKED", "lease + heartbeat + fencing"]),
            ("STEP 3", "Transcribe", ["one request per channel", "channel becomes speaker role"]),
            ("STEP 4", "Protect", ["AES-256-GCM raw transcript", "mask TCKN, IBAN, phone, names"]),
        ], DATA),
        ("3  CRM delivery", 488, [
            ("STEP 5", "Extract", ["meeting / complaint rules", "runs on masked text only"]),
            ("OUTBOX", "Outbox row", ["derived Idempotency-Key", "tenant:booking:call:slot"]),
            ("STEP 6", "Reconcile + send", ["GET first, adopt if found", "POST with jittered retry"]),
        ], OK),
    ]
    for label, y, cards, accent in layers:
        s.rect(40, y - 16, 1200, ch + 32, fill="#F3EFE8", stroke=LINE, r=14)
        s.text(60, y + 6, label, 13, accent, 700)
        for i, (num, title, lines) in enumerate(cards):
            x = 150 + i * (cw + gap)
            s.card(x, y + 14, cw, ch - 14, num, title, lines, accent)
            if i < len(cards) - 1:
                s.line(x + cw + 4, y + 14 + (ch - 14) / 2, x + cw + gap - 4,
                       y + 14 + (ch - 14) / 2)
    # the CRM is outside the system boundary
    s.rect(1104, 520, 136, 90, fill="#FFF4EC", stroke=CHAOS, r=10, dash="6 4")
    s.text(1172, 552, "External CRM", 15, CHAOS, 650, anchor="middle")
    s.text(1172, 578, "429, 500, stalls,", 11.5, MUTED, anchor="middle")
    s.text(1172, 598, "dropped replies", 11.5, MUTED, anchor="middle")
    cy = 14 + 488 + (ch - 14) / 2
    s.line(150 + 2 * (cw + gap) + cw + 4, cy, 1100, cy, color=CHAOS, marker="ac", dash="6 4")
    # hand-offs between layers
    s.path("M 910 240 L 910 270 L 290 270 L 290 314", dash="2 4")
    s.path("M 910 428 L 910 458 L 290 458 L 290 502", dash="2 4")

    s.rect(40, 672, 1200, 148, fill="#EEF2F8", stroke=DATA, r=14)
    s.text(60, 702, "PostgreSQL", 15, DATA, 700)
    s.text(215, 702, "single source of truth for state, jobs, transcripts, outbox and API keys",
           13.5, MUTED)
    facts = [("FORCE ROW LEVEL SECURITY", "on every table; no tenant context is an SQL error"),
             ("3 roles", "owner, worker, tenant reader: none SUPERUSER, BYPASSRLS or DELETE"),
             ("Column privileges", "tenant reader has no grant on raw transcripts or the queue"),
             ("Hashed API keys", "tenant comes from the key, never from a flag")]
    for i, (k, v) in enumerate(facts):
        x = 60 + (i % 2) * 590
        y = 738 + (i // 2) * 44
        s.text(x, y, k, 13, INK, 650)
        s.text(x, y + 22, v, 12, MUTED)
    s.save("architecture.svg")


# ----------------------------------------------------------- exactly once

def exactly_once() -> None:
    s = Svg(1280, 700, "Why a lost CRM response never creates a duplicate booking",
            "Sequence between worker, database outbox and CRM across a lost reply and a retry.")
    s.text(48, 56, "Exactly-once delivery to an unreliable CRM", 26, INK, 750)
    s.text(48, 82, "The CRM committed the booking but the reply never arrived. The worker cannot "
                   "know, so it asks before it writes again.", 14, MUTED)
    for name, x in (("Worker", 200), ("PostgreSQL outbox", 640), ("CRM", 1080)):
        s.rect(x - 90, 112, 180, 40, fill=CARD, r=8)
        s.text(x, 138, name, 14, INK, 650, anchor="middle")
        s.line(x, 156, x, 660, color=LINE, marker=None, dash="4 4", sw=1.4)

    def msg(y, x1, x2, label, n, color=MUTED, marker="a", dash=None, note=None):
        s.line(x1, y, x2, y, color=color, marker=marker, dash=dash)
        mid = (x1 + x2) / 2
        s.text(mid, y - 9, label, 12.5, INK, 600, anchor="middle", mono=True)
        if note:
            s.text(mid, y + 18, note, 11.5, MUTED, anchor="middle")
        s.add(f'<circle cx="{min(x1, x2) - 22}" cy="{y}" r="11" fill="{color}"/>')
        s.text(min(x1, x2) - 22, y + 4, n, 11.5, "#FFFFFF", 700, anchor="middle")

    msg(196, 200, 640, "INSERT outbox (key K)", "1", DATA,
        note="same transaction as the step's output")
    msg(262, 200, 1080, "GET /bookings", "2", note="not there yet: safe to create")
    msg(328, 200, 1080, "POST /bookings  Idempotency-Key: K", "3")
    s.text(1080, 356, "booking committed", 12, OK, 650, anchor="middle")
    msg(404, 1080, 200, "no reply (connection dropped, or worker killed)", "4", CHAOS, "ac", dash="6 4")
    msg(470, 200, 1080, "GET /bookings", "5", note="the next attempt reconciles before it writes")
    msg(536, 1080, 200, "booking found for this call", "6", OK, "ao")
    msg(602, 200, 640, "mark done, adopt CRM id", "7", OK, "ao",
        note="result: one booking, the retry cost nothing")
    s.rect(716, 566, 524, 116, fill="#F3EFE8", stroke=LINE, r=10)
    s.text(736, 594, "Retry inside one attempt: same key, same body.", 12.5, INK, 650)
    s.text(736, 618, "The CRM replays its first answer (Idempotent-Replayed).", 12.5, MUTED)
    s.text(736, 646, "Slot taken meanwhile (409): next free slot, NEW key.", 12.5, MUTED)
    s.text(736, 670, "A key is never reused with a different body.", 12.5, MUTED)
    s.save("exactly-once.svg")


# ----------------------------------------------------------- tenant model

def tenant_model() -> None:
    s = Svg(1280, 520, "Tenant isolation fails closed",
            "An API key sets the tenant context; a query without context raises an SQL error.")
    s.text(48, 56, "Tenant isolation that fails closed", 26, INK, 750)
    s.text(48, 82, "The tenant is derived from a hashed API key. The database refuses to answer "
                   "without it.", 14, MUTED)

    s.card(48, 120, 280, 140, "REQUEST", "API key",
           ["cv_... (shown once)", "stored as SHA-256 only", "--tenant flag is only checked"])
    s.card(388, 120, 300, 140, "TRANSACTION", "Tenant context",
           ["set for this transaction only", "role: app_user", "no context: an SQL error"], OK)
    s.card(748, 120, 300, 140, "POLICY", "FORCE RLS",
           ["tenant_id must match context", "applies to the table owner too", "no BYPASSRLS anywhere"])
    s.line(332, 190, 384, 190)
    s.line(692, 190, 744, 190)

    s.rect(48, 300, 560, 170, fill="#FFF4EC", stroke=CHAOS, r=12)
    s.text(70, 332, "No context", 15, CHAOS, 700)
    s.text(70, 360, "SELECT * FROM transcripts;", 13, INK, mono=True)
    s.text(70, 392, "ERROR: tenant context is not set", 12.5, CHAOS, mono=True)
    s.text(70, 422, "Not an empty result: a forgotten filter", 12.5, MUTED)
    s.text(70, 446, "fails loudly instead of leaking quietly.", 12.5, MUTED)

    s.rect(660, 300, 572, 170, fill="#EEF6F0", stroke=OK, r=12)
    s.text(682, 332, "What a tenant can and cannot read", 15, OK, 700)
    rows = [("masked transcript", "yes"), ("raw transcript column", "no grant"),
            ("job queue", "no grant"), ("another tenant's rows", "invisible"),
            ("ciphertext copied to another row", "does not decrypt (AAD)")]
    for i, (k, v) in enumerate(rows):
        s.text(682, 364 + i * 22, k, 12.5, INK)
        s.text(1210, 364 + i * 22, v, 12.5, MUTED, anchor="end")
    s.save("tenant-model.svg")


def social() -> None:
    """1280x640 card for the GitHub social preview / LinkedIn link preview."""
    s = Svg(1280, 640, "Callvault", "No recording lost, no duplicate booking, tenants isolated.")
    s.rect(0, 0, 1280, 12, fill=CHAOS, stroke=CHAOS, r=0)
    s.text(96, 230, "Callvault", 96, INK, 750)
    s.text(96, 304, "A call-recording pipeline that does not lose calls", 32, INK)
    s.text(96, 348, "and does not double-book.", 32, INK)
    s.text(96, 410, "PostgreSQL row-level security  /  crash-safe queue  /  exactly-once CRM delivery",
           20, MUTED)
    for i, (k, v, c) in enumerate([(str(TEST_COUNT), "tests", DATA), ("0", "lost", OK),
                                   ("0", "duplicates", OK)]):
        x = 96 + i * 200
        s.text(x, 520, k, 56, c, 750)
        s.text(x, 552, v, 20, MUTED)
    s.save("social-preview.svg")


def dark_variants() -> None:
    from darkmode import to_dark
    for name in ("hero", "architecture", "exactly-once", "tenant-model"):
        src = OUT / f"{name}.svg"
        (OUT / f"{name}-dark.svg").write_text(to_dark(src.read_text(encoding="utf-8")),
                                              encoding="utf-8")


if __name__ == "__main__":
    social()
    hero()
    architecture()
    exactly_once()
    tenant_model()
    dark_variants()
