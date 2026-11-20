"""Write the synthetic sample dataset: samples/manifest.json and samples/*.wav.

The audio is stereo tones, not speech. It exists to exercise ingestion, validation and
de-duplication end to end with no real call data: six calls, one of them a byte-identical
copy of another and one truncated. Speech-to-text and the CRM path are shown by
`python -m app.cli demo-chaos`, which scripts the transcripts.

    python scripts/make_samples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.wavs import wav_bytes  # noqa: E402

OUT = ROOT / "samples"
TENANT = {"id": "harbor", "name": "Harbor Call Center (synthetic)"}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    recordings = []
    originals: dict[str, bytes] = {}

    def add(rec_id: str, audio: bytes, minute: int) -> None:
        (OUT / f"{rec_id}.wav").write_bytes(audio)
        recordings.append({
            "id": rec_id, "tenant_id": TENANT["id"], "file": f"{rec_id}.wav",
            "language": "tr", "channel": "phone",
            "recorded_at": f"2026-10-30T09:{minute:02d}:00+03:00",
        })

    for i in range(1, 5):
        originals[f"call_{i:02d}"] = wav_bytes(seconds=4, freq=200 + 60 * i)
        add(f"call_{i:02d}", originals[f"call_{i:02d}"], minute=i)
    add("call_05_duplicate_of_01", originals["call_01"], minute=5)
    add("call_06_truncated", wav_bytes(seconds=10, freq=900)[:2048], minute=6)

    (OUT / "manifest.json").write_text(
        json.dumps({"tenants": [TENANT], "recordings": recordings}, indent=2) + "\n",
        encoding="utf-8")
    print(f"wrote {len(recordings)} recordings to {OUT}")


if __name__ == "__main__":
    main()
