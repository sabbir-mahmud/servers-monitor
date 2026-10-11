"""Time-based one-time passwords (RFC 6238, the authenticator-app standard).

Small and fully specified, so it's implemented here on the standard library and tested
against the RFC's own vectors rather than pulling in a dependency.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6
WINDOW = 1  # accept the previous/next 30 s step too (clock drift)


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    return base64.b32decode(padded)


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def totp(secret_b32: str, at: float | None = None, digits: int = DIGITS) -> str:
    return hotp(_key(secret_b32), int((time.time() if at is None else at) // STEP), digits)


def verify(
    secret_b32: str, code: str, *, at: float | None = None, last_step: int = 0
) -> int | None:
    """The matching time step, or None. Steps at or before `last_step` are refused, so a
    code can't be replayed within its window."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now_step = int((time.time() if at is None else at) // STEP)
    key = _key(secret_b32)
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):
        if step > last_step and hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret_b32, "issuer": issuer})


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2))
        for _ in range(n)
    ]
