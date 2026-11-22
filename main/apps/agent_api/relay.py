"""The Redis relay between this dashboard and its agents.

Neither side accepts inbound connections: both dial out over TLS to Redis (e.g. Upstash)
and exchange JSON messages through two lists per server:

    <prefix>:a:<server id>:cmd   dashboard -> agent   cmd / cancel / cfg
    <prefix>:a:<server id>:in    agent -> dashboard   hb / ack / res

Every agent authenticates as its own ACL user (`sm-<server id>`) allowed only RPUSH, BLPOP,
LTRIM and PING on its own two keys. The dashboard uses the admin connection in REDIS_URL,
which never leaves this machine. Revoking a server is deleting (or disabling) its ACL user.

Devices (computers and phones) use the relay too, so the dashboard has no device API:
see "devices" below.
"""

import base64
import gzip
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from functools import cache

import redis
from django.conf import settings

from . import signing, usage

logger = logging.getLogger(__name__)

AGENT_COMMANDS = ("+rpush", "+blpop", "+ltrim", "+ping")
# Anything bigger is dropped unread: a single agent must not be able to make the bridge
# parse megabytes (output chunks are at most 64 KiB; heartbeats a few KiB).
MAX_MESSAGE_BYTES = 256 * 1024


class RelayError(Exception):
    """Redis is unreachable or refused an admin operation."""


class _CountingRedis(redis.Redis):
    """Counts every command for the usage meter (apps/agent_api/usage.py)."""

    def execute_command(self, *args, **options):
        usage.count_dashboard()
        return super().execute_command(*args, **options)


@cache
def client() -> redis.Redis:
    if not settings.REDIS_URL:
        raise RelayError("REDIS_URL is not configured")
    return _CountingRedis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=settings.RELAY_BLOCK_SECONDS + 15,
        health_check_interval=60,
        retry_on_timeout=False,
    )


# The relay process touches this file every few seconds; the web process reads its age to
# warn when nobody is receiving agent messages. A file next to the SQLite database works
# across both processes with no extra I/O.
STALE_AFTER_SECONDS = 30


def _alive_marker():
    return settings.SQLITE_PATH.parent / ".relay-alive"


def mark_alive() -> None:
    try:
        _alive_marker().touch()
    except OSError as exc:
        logger.warning("Could not write relay liveness marker: %s", exc)


def last_alive_at():
    """When the relay process last marked itself alive (None if it never has)."""
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(_alive_marker().stat().st_mtime, tz=UTC)
    except OSError:
        return None


def is_alive() -> bool:
    try:
        return time.time() - _alive_marker().stat().st_mtime < STALE_AFTER_SECONDS
    except OSError:
        return False


def username(server) -> str:
    return f"sm-{server.pk}"


def keys(server) -> tuple[str, str]:
    base = f"{settings.RELAY_KEY_PREFIX}:a:{server.pk}"
    return f"{base}:cmd", f"{base}:in"


def inbox_keys(server_ids) -> dict[str, str]:
    """Map each server's agent->dashboard key back to its server id."""
    return {f"{settings.RELAY_KEY_PREFIX}:a:{pk}:in": str(pk) for pk in server_ids}


# --------------------------------------------------------------------------- credentials


def _new_password(user: str) -> str:
    """Upstash only accepts ACL passwords minted by its `ACL GENTOKEN` extension. Redis,
    Valkey and other servers don't have it (they reply "unknown subcommand", worded
    differently per server) but accept any strong string. Whatever the refusal, fall back;
    if the server then rejects the password too, ACL SETUSER reports why."""
    try:
        return client().execute_command("ACL", "GENTOKEN", user)
    except redis.ResponseError:
        return "smk_" + secrets.token_urlsafe(32)


def provision(server) -> str:
    """Create (or reset) the server's ACL user with a fresh password and a fresh signing
    key. Returns the agent's connection URL; it contains both secrets, neither of which the
    dashboard stores. The caller saves `server.signing_salt`."""
    server.signing_salt = signing.new_salt()
    user = username(server)
    cmd_key, in_key = keys(server)
    try:
        password = _new_password(user)
        client().execute_command(
            "ACL", "SETUSER", user, "reset", "on" if server.is_enabled else "off",
            f">{password}", "resetchannels", f"~{cmd_key}", f"~{in_key}", "-@all", *AGENT_COMMANDS,
        )  # fmt: skip
    except redis.RedisError as exc:
        raise RelayError(f"could not create the agent's Redis user: {exc}") from exc
    return agent_url(user, password, signing.server_key(server))


def agent_url(user: str, password: str, key: bytes | None = None) -> str:
    admin = urllib.parse.urlsplit(settings.REDIS_URL)
    host = settings.RELAY_PUBLIC_HOST or admin.hostname
    port = admin.port or 6379
    scheme = "rediss" if admin.scheme == "rediss" else "redis"
    url = f"{scheme}://{user}:{urllib.parse.quote(password, safe='')}@{host}:{port}"
    # The signing key rides in the query string: the agent strips it before talking to Redis.
    return f"{url}/?key={key.hex()}" if key else url


def set_enabled(server) -> None:
    try:
        client().execute_command(
            "ACL", "SETUSER", username(server), "on" if server.is_enabled else "off"
        )
    except redis.RedisError as exc:
        logger.warning("Could not toggle relay user for %s: %s", server.name, exc)


def deprovision(server) -> None:
    try:
        client().execute_command("ACL", "DELUSER", username(server))
        client().delete(*keys(server))
    except redis.RedisError as exc:
        logger.warning("Could not remove relay user for %s: %s", server.name, exc)


# --------------------------------------------------------------------------- messages


def _seal(server, message: dict) -> dict:
    key = signing.server_key(server)
    return signing.sign(message, key) if key else message


def command_message(command) -> str:
    """Deterministic, signed JSON for a command, so a queued copy can be found again with
    LREM (HMAC is deterministic, so re-signing gives the same bytes)."""
    message = _seal(
        command.server,
        {
            "t": "cmd",
            "id": str(command.pk),
            "action": command.action,
            "args": command.args,
            "timeout": command.timeout_seconds,
            "exp": int(command.deliver_by.timestamp()),
        },
    )
    return json.dumps(message, sort_keys=True, separators=(",", ":"))


def push(server, message: dict) -> None:
    sealed = _seal(server, message)
    client().rpush(keys(server)[0], json.dumps(sealed, sort_keys=True, separators=(",", ":")))


def push_command(command) -> None:
    client().rpush(keys(command.server)[0], command_message(command))


def unqueue_command(command) -> bool:
    """Remove a not-yet-popped command from the agent's mailbox. True if it was still
    there, which proves the agent never received it."""
    return bool(client().lrem(keys(command.server)[0], 1, command_message(command)))


def receive(
    server_ids, timeout: int, device_ids=(), pairing: bool = False
) -> tuple[str, str, dict] | None:
    """Block until any agent or device sends something. Returns ("server", "device" or
    "pairing", its id ("" for pairing), message) or None. `pairing`: also listen for devices
    asking to pair (while any device has a code out)."""
    mapping = {key: ("server", pk) for key, pk in inbox_keys(server_ids).items()}
    mapping |= {device_inbox_key(pk): ("device", str(pk)) for pk in device_ids}
    if pairing:
        mapping[pair_inbox_key()] = ("pairing", "")
    if not mapping:
        time.sleep(min(timeout, 5))
        return None
    reply = client().blpop(list(mapping), timeout=timeout)
    if not reply:
        return None
    key, raw = reply
    if len(raw) > MAX_MESSAGE_BYTES:
        logger.warning("Dropping oversized relay message (%d bytes) from %s", len(raw), key)
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        logger.warning("Dropping malformed relay message from %s", key)
        return None
    if not isinstance(message, dict):
        return None
    return (*mapping[key], message)


# --------------------------------------------------------------------------- devices
#
# Computers and phones check in through the relay, so they report from anywhere while the
# dashboard stays private. Each paired device has its own ACL user (`sm-d-<device id>`):
#
#     <prefix>:d:<device id>:in    device -> dashboard   ci (check-in), sos
#     <prefix>:d:<device id>:cfg   dashboard -> device   settings (computers; SET, the device GETs)
#     <prefix>:d:<device id>:bl    dashboard -> device   blocklist (computers; SET, gzip)
#
# Computers connect with the Redis protocol, like server agents, so any Redis 6+ works. A
# phone app can't open a Redis connection, so it uses Upstash's HTTPS REST API with a REST
# token from `ACL RESTTOKEN` (an Upstash extension); phones need Upstash. Everything either
# side writes is signed with the key the device made when it paired, which Redis never sees.

PHONE_COMMANDS = ("+rpush", "+ping")
COMPUTER_COMMANDS = ("+rpush", "+get", "+ping")


def device_username(device) -> str:
    return f"sm-d-{device.pk}"


def device_inbox_key(device_id) -> str:
    return f"{settings.RELAY_KEY_PREFIX}:d:{device_id}:in"


def device_config_key(device_id) -> str:
    return f"{settings.RELAY_KEY_PREFIX}:d:{device_id}:cfg"


def device_blocklist_key(device_id) -> str:
    return f"{settings.RELAY_KEY_PREFIX}:d:{device_id}:bl"


def rest_url() -> str:
    """Where phones reach the relay over HTTPS: RELAY_REST_URL, or the Upstash host in
    REDIS_URL. Empty when this dashboard's Redis has no REST API."""
    if settings.RELAY_REST_URL:
        return settings.RELAY_REST_URL.rstrip("/")
    host = settings.RELAY_PUBLIC_HOST or urllib.parse.urlsplit(settings.REDIS_URL).hostname or ""
    return f"https://{host}" if host.endswith(".upstash.io") else ""


def provision_device(device) -> dict:
    """Create (or reset) the phone's ACL user with a fresh password and REST token. Returns
    what the phone needs: {"url", "token", "inbox"}. Sets `device.relay_salt` (the caller
    saves it), which marks the device as one the bridge listens to. Resetting cuts off a
    phone that had the old token."""
    url = rest_url()
    if not url:
        raise RelayError(
            "phones check in through an Upstash Redis relay, and this dashboard's REDIS_URL "
            "isn't Upstash"
        )
    device.relay_salt = signing.new_salt()
    user, inbox = device_username(device), device_inbox_key(device.pk)
    try:
        password = client().execute_command("ACL", "GENTOKEN", user)
        client().execute_command(
            "ACL", "SETUSER", user, "reset", "on", f">{password}", "resetchannels",
            f"~{inbox}", "-@all", *PHONE_COMMANDS,
        )  # fmt: skip
        token = client().execute_command("ACL", "RESTTOKEN", user, password)
    except redis.RedisError as exc:
        raise RelayError(f"could not create the phone's Redis user: {exc}") from exc
    return {"url": url, "token": token, "inbox": inbox}


def provision_computer(device) -> dict:
    """Create (or reset) the computer's ACL user: RPUSH to its inbox, GET its settings and
    blocklist. Returns {"url", "inbox", "config", "blocklist"}; the URL holds the password,
    which the dashboard doesn't keep. Sets `device.relay_salt` (the caller saves it)."""
    device.relay_salt = signing.new_salt()
    user = device_username(device)
    keys_ = (device_inbox_key(device.pk), device_config_key(device.pk),
             device_blocklist_key(device.pk))  # fmt: skip
    try:
        password = _new_password(user)
        client().execute_command(
            "ACL", "SETUSER", user, "reset", "on", f">{password}", "resetchannels",
            *(f"~{k}" for k in keys_), "-@all", *COMPUTER_COMMANDS,
        )  # fmt: skip
    except redis.RedisError as exc:
        raise RelayError(f"could not create the computer's Redis user: {exc}") from exc
    return {"url": agent_url(user, password), "inbox": keys_[0], "config": keys_[1],
            "blocklist": keys_[2]}  # fmt: skip


def deprovision_device(device) -> None:
    try:
        client().execute_command("ACL", "DELUSER", device_username(device))
        client().delete(
            device_inbox_key(device.pk), device_config_key(device.pk),
            device_blocklist_key(device.pk),
        )  # fmt: skip
    except (redis.RedisError, RelayError) as exc:
        logger.warning("Could not remove the relay user for %s: %s", device.name, exc)


def _sealed_for(device, body: str) -> str:
    key = signing.device_relay_key(device)
    if key is None:
        raise RelayError(f"{device.name} has no signing key: pair it again")
    return json.dumps({"body": body, "sig": signing.sign_text(key, body)},
                      separators=(",", ":"))  # fmt: skip


def config_digest(body: str) -> str:
    """What a computer echoes back as `config` in its check-ins: the settings it has."""
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def publish_config(device, body: str) -> None:
    """Leave the computer's settings (JSON text) where it GETs them at each check-in."""
    try:
        client().set(device_config_key(device.pk), _sealed_for(device, body))
    except redis.RedisError as exc:
        raise RelayError(f"could not update {device.name}'s settings: {exc}") from exc


def publish_blocklist(device, version: str, lines: list[str]) -> None:
    """The hosts lines for the computer, gzipped (a few MB of names shrink well)."""
    packed = gzip.compress("\n".join(lines).encode(), mtime=0)
    body = json.dumps({"version": version, "gz": base64.b64encode(packed).decode()},
                      separators=(",", ":"))  # fmt: skip
    try:
        client().set(device_blocklist_key(device.pk), _sealed_for(device, body))
    except redis.RedisError as exc:
        raise RelayError(f"could not update {device.name}'s blocklist: {exc}") from exc


# --------------------------------------------------------------------------- pairing
#
# Devices pair through the relay too: neither the phone app nor the computer agent talks
# to the dashboard. Each pairs with a shared, tightly limited login that may only RPUSH
# pairing requests to one inbox and GET answers, each under a random key it picked:
#
#     <prefix>:pair:in             device -> dashboard   {"t": "pair", "body": "…"}
#     <prefix>:pair:out:<nonce>    dashboard -> device   {"state": "paired" | "preview" | "error", …}
#
# Phones use `sm-app` (in the app's .env; make it with `manage.py phone_app_login`).
# Computers use `sm-pc`, which the dashboard makes on demand and puts in the install
# script it serves. The request carries the code; the answer carries the device's own
# login (above). Answers expire after a few minutes. Neither login can read the inbox,
# list keys or write answers, so holding one only lets someone guess codes, which the
# bridge limits.

APP_USERNAME = "sm-app"
COMPUTER_PAIR_USERNAME = "sm-pc"
PAIR_COMMANDS = ("+rpush", "+get", "+ping")
ANSWER_SECONDS = 10 * 60  # long enough for the device to read it, then gone
_COMPUTER_PAIR_STATE = "relay.computer_pair_login"


def pair_inbox_key() -> str:
    return f"{settings.RELAY_KEY_PREFIX}:pair:in"


def pair_answer_key(nonce: str) -> str:
    return f"{settings.RELAY_KEY_PREFIX}:pair:out:{nonce}"


def _set_pair_user(user: str, password: str) -> None:
    client().execute_command(
        "ACL", "SETUSER", user, "reset", "on", f">{password}", "resetchannels",
        f"~{pair_inbox_key()}", f"~{pair_answer_key('*')}", "-@all", *PAIR_COMMANDS,
    )  # fmt: skip


def provision_app_login() -> dict:
    """Create (or reset) the phone app's shared login. Returns what goes in the app's .env:
    {"url", "token", "prefix"}. Resetting it stops apps built with the old token pairing
    (phones already paired keep checking in with their own logins)."""
    url = rest_url()
    if not url:
        raise RelayError(
            "phones pair and check in through an Upstash Redis relay, and this dashboard's "
            "REDIS_URL isn't Upstash"
        )
    try:
        password = client().execute_command("ACL", "GENTOKEN", APP_USERNAME)
        _set_pair_user(APP_USERNAME, password)
        token = client().execute_command("ACL", "RESTTOKEN", APP_USERNAME, password)
    except redis.RedisError as exc:
        raise RelayError(f"could not create the phone app's login: {exc}") from exc
    return {"url": url, "token": token, "prefix": settings.RELAY_KEY_PREFIX}


def computer_pair_url(reset: bool = False) -> str:
    """The relay address computers pair with (`rediss://sm-pc:…@host:port/?prefix=sm`), for
    the install scripts. Made the first time it's needed and kept (encrypted) so every
    script hands out the same one; `reset` replaces it, which stops scripts already
    downloaded from pairing. Raises RelayError."""
    from django.core.cache import cache

    from apps.core import crypto
    from apps.core.models import SystemState

    # Kept in this process's memory too: install scripts are public, so serving one
    # should cost no database work at all.
    cache_key = f"{_COMPUTER_PAIR_STATE}:{settings.REDIS_URL}:{settings.RELAY_KEY_PREFIX}"
    if not reset and (url := cache.get(cache_key)):
        return url
    password = ""
    stored = SystemState.get(_COMPUTER_PAIR_STATE) or {}
    if stored.get("password") and not reset:
        try:
            password = crypto.decrypt(stored["password"])
        except Exception:  # SECRET_KEY rotated without a fallback: make a new one
            password = ""
    if not password:
        try:
            password = _new_password(COMPUTER_PAIR_USERNAME)
            _set_pair_user(COMPUTER_PAIR_USERNAME, password)
        except redis.RedisError as exc:
            raise RelayError(f"could not create the computers' pairing login: {exc}") from exc
        SystemState.put(_COMPUTER_PAIR_STATE, {"password": crypto.encrypt(password)})
    url = agent_url(COMPUTER_PAIR_USERNAME, password)
    url = f"{url}/?prefix={urllib.parse.quote(settings.RELAY_KEY_PREFIX, safe='')}"
    cache.set(cache_key, url, 3600)
    return url


def answer_pairing(nonce: str, answer: dict) -> None:
    """Leave the answer where the device polls for it. Raises RelayError."""
    data = json.dumps(answer, separators=(",", ":"))
    try:
        client().set(pair_answer_key(nonce), data, ex=ANSWER_SECONDS)
    except redis.RedisError as exc:
        raise RelayError(f"could not answer the device: {exc}") from exc
