"""Pairing codes and device tokens.

A parent adds a device and gets a **pairing code**. The code has no time limit: it works
until a device uses it, or until the parent makes a new one (which cancels the old one).
Pairing happens on the device itself (typing the code or scanning the QR code), so nobody
can be enrolled silently.

Pairing gives the device a **token** that never expires. It stays valid until the device
is reset (the app loses it, so the device simply stops checking in) or the parent removes
the device or pairs it again, which replaces the token.

Both are stored only as SHA-256 digests. Computer codes are 8 characters from a 31-letter
alphabet (~40 bits): the pairing endpoint is rate-limited per IP, so guessing one is
hopeless. Phone codes are 6 digits, typed into the app: they expire after 15 minutes and
the relay limits wrong guesses across all phones.
"""

import hashlib
import secrets

# No 0/O, 1/I/L, U/V: codes are read aloud and typed on phones.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTWXYZ"
CODE_LENGTH = 8
TOKEN_PREFIX = "ngd_"


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_code(raw: str) -> str:
    """What the device typed -> the canonical code: case, spaces and dashes don't matter."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def new_code() -> tuple[str, str]:
    """(code to show, e.g. "K7PQ-3MXA", digest to store)."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return f"{code[:4]}-{code[4:]}", digest(code)


# Phones: a 6-digit code typed into the app. Short, so it expires and the relay limits
# guesses (apps/agent_api/bridge.py); unique among codes that are out.
PHONE_CODE_LENGTH = 6
PHONE_CODE_MINUTES = 15


def new_phone_code() -> tuple[str, str]:
    """(code to show, e.g. "482 913", digest to store)."""
    code = "".join(secrets.choice("0123456789") for _ in range(PHONE_CODE_LENGTH))
    return f"{code[:3]} {code[3:]}", digest(code)


def new_token() -> tuple[str, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, digest(raw)
