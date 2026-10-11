"""Web Push to browsers, on the `cryptography` package the dashboard already uses.

- Payload encryption: RFC 8291 (ECDH P-256 + HKDF-SHA256 + AES-128-GCM, "aes128gcm"
  content coding from RFC 8188), so the push service only ever sees ciphertext.
- Sender identity: RFC 8292 VAPID, an ES256-signed JWT per push service. The key pair is
  generated once per install and kept (private half encrypted) in SystemState.
"""

import base64
import json
import os
import time
import urllib.parse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000  # push services accept ~4 KB of ciphertext


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_bytes(key) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    payload: bytes, ua_public: bytes, auth_secret: bytes, *, as_private=None, salt=None
) -> bytes:
    """The request body for one push (RFC 8291 §3.4, a single aes128gcm record).
    `as_private`/`salt` are fixed only by tests; normally both are fresh per message."""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _public_bytes(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02: last record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private, auth_secret: bytes) -> bytes:
    """The browser's side, for tests: proves `encrypt` round-trips."""
    salt, idlen = body[:16], body[20]
    as_public, ciphertext = body[21 : 21 + idlen], body[21 + idlen :]
    ua_public = _public_bytes(ua_private.public_key())
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_private.exchange(ec.ECDH(), as_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, ciphertext, None).rstrip(b"\x00")[:-1]


# --------------------------------------------------------------------------- VAPID


def vapid_keys() -> tuple:
    """(private key, public key as base64url) for this install, created on first use."""
    from apps.core import crypto
    from apps.core.models import SystemState

    stored = SystemState.get("vapid")
    if not stored:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()  # fmt: skip
        SystemState.objects.get_or_create(
            key="vapid", defaults={"value": {"private": crypto.encrypt(pem)}}
        )  # two processes racing: the first one wins, both read it back
        stored = SystemState.get("vapid")
    key = serialization.load_pem_private_key(crypto.decrypt(stored["private"]).encode(), None)
    return key, b64u(_public_bytes(key.public_key()))


def vapid_header(endpoint: str, subject: str, now=None) -> str:
    key, public = vapid_keys()
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600),
              "sub": subject}  # fmt: skip
    signing_input = (
        b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        + "."
        + b64u(json.dumps(claims, separators=(",", ":")).encode())
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{signing_input}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={public}"
