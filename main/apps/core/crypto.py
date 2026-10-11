"""Encryption for secrets at rest (bot tokens, webhook URLs, pending enrolment credentials).

Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from SECRET_KEY via HKDF, so there is
no extra key to manage. SECRET_KEY_FALLBACKS keep old ciphertexts readable during a key
rotation; re-saving a record re-encrypts it under the current key.
"""

import base64
import logging
from functools import cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

logger = logging.getLogger(__name__)

PREFIX = "enc1:"


def _derive(secret: str) -> Fernet:
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"server-monitor", info=b"at-rest-secrets/v1"
    ).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


@cache
def _fernet(secret_key: str, fallbacks: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([_derive(secret_key), *(_derive(k) for k in fallbacks)])


def _keys() -> MultiFernet:
    return _fernet(settings.SECRET_KEY, tuple(settings.SECRET_KEY_FALLBACKS))


def encrypt(plaintext: str) -> str:
    return PREFIX + _keys().encrypt(plaintext.encode()).decode()


def decrypt(stored: str) -> str:
    """Plaintext for an encrypted value; legacy unencrypted values pass through unchanged.
    If no current or fallback key can open it, log and return "" (never the ciphertext)."""
    if not stored or not stored.startswith(PREFIX):
        return stored
    try:
        return _keys().decrypt(stored[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.error(
            "Could not decrypt a stored secret: SECRET_KEY changed without keeping the old "
            "one in SECRET_KEY_FALLBACKS. Re-enter the affected secret."
        )
        return ""
