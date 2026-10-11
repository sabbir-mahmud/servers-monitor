"""Agent tokens: shown once at enrolment, stored only as a SHA-256 digest."""

import hashlib
import secrets

TOKEN_PREFIX = "smk_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return (raw token, digest to store, short prefix to display)."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw), raw[len(TOKEN_PREFIX) : len(TOKEN_PREFIX) + 8]
