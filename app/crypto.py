"""Application-level encryption of raw (unmasked) transcripts.

AES-256-GCM. The associated data is "<tenant_id>/<recording_id>", so a ciphertext only
decrypts for the row it was written for: copying tenant A's blob into tenant B's row
(by a bug or an attacker with write access) fails authentication instead of leaking.

Key source, in order: TRANSCRIPT_KEY (urlsafe base64, 32 bytes) or the file at
TRANSCRIPT_KEY_FILE. `migrate` creates that file (0600) if neither exists, so a clean
`docker compose up` works without a manual step; the key lives in a Docker volume, not
in the repository.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"v1"
DEFAULT_KEY_FILE = ".keys/transcript.key"


class KeyMissing(Exception):
    pass


class DecryptionFailed(Exception):
    pass


class TranscriptCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("transcript key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_env(cls) -> TranscriptCipher:
        raw = os.environ.get("TRANSCRIPT_KEY")
        if not raw:
            path = Path(os.environ.get("TRANSCRIPT_KEY_FILE", DEFAULT_KEY_FILE))
            if not path.exists():
                raise KeyMissing(f"no TRANSCRIPT_KEY and no key file at {path}; run `migrate`")
            raw = path.read_text().strip()
        return cls(base64.urlsafe_b64decode(raw))

    def encrypt(self, plaintext: bytes, aad: str) -> bytes:
        nonce = os.urandom(12)
        return VERSION + nonce + self._aead.encrypt(nonce, plaintext, aad.encode())

    def decrypt(self, blob: bytes, aad: str) -> bytes:
        blob = bytes(blob)
        if blob[:2] != VERSION:
            raise DecryptionFailed("unknown ciphertext version")
        try:
            return self._aead.decrypt(blob[2:14], blob[14:], aad.encode())
        except InvalidTag:
            raise DecryptionFailed("authentication failed (wrong key or wrong row)") from None


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def ensure_key_file(path: str | Path | None = None) -> bool:
    """Create the key file if missing. Returns True if it was created."""
    if os.environ.get("TRANSCRIPT_KEY"):
        return False
    path = Path(path or os.environ.get("TRANSCRIPT_KEY_FILE", DEFAULT_KEY_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(new_key())
    return True
