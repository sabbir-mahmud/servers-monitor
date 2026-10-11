"""End-to-end message signing between the dashboard and each agent.

Redis (Upstash) only carries messages; it must not be able to *author* them. Every message
is signed with HMAC-SHA256 under a per-server key that never touches Redis:

    key = HMAC(SECRET_KEY, "agent-signing:v1:<server id>:<salt>")

The salt is random per credential issue (rotating credentials rotates the key), and is the
only thing stored. The agent receives the key once, inside its connection string. Without
it, someone holding the Redis admin password (a leaked .env, the Upstash account) can still
read traffic and drop messages, but can no longer run a single command on any server or
forge a result.

Changing SECRET_KEY changes every key: rotate agent credentials afterwards.
"""

import hashlib
import hmac
import json
import secrets

from django.conf import settings

VERSION = "v1"


def new_salt() -> str:
    return secrets.token_hex(16)


def key_for(server_id, salt: str, secret: str | None = None) -> bytes:
    secret = (secret or settings.SECRET_KEY).encode()
    return hmac.new(
        secret, f"agent-signing:{VERSION}:{server_id}:{salt}".encode(), hashlib.sha256
    ).digest()


def canonical(message: dict) -> bytes:
    body = {k: v for k, v in message.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(message: dict, key: bytes) -> dict:
    return {**message, "sig": hmac.new(key, canonical(message), hashlib.sha256).hexdigest()}


def verify(message: dict, key: bytes) -> bool:
    sig = message.get("sig")
    if not isinstance(sig, str):
        return False
    expected = hmac.new(key, canonical(message), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def server_key(server) -> bytes | None:
    """None for servers enrolled before signing existed (legacy, unsigned)."""
    return key_for(server.pk, server.signing_salt) if server.signing_salt else None


def verify_from_server(server, message: dict) -> bool:
    """Agent → dashboard. Also accepts keys derived from SECRET_KEY_FALLBACKS, so a key
    rotation doesn't silently drop every heartbeat while agents are being re-keyed."""
    if not server.signing_salt:
        return True  # legacy agent: accepted, and flagged on its page
    for secret in (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS):
        if verify(message, key_for(server.pk, server.signing_salt, secret)):
            return True
    return False
