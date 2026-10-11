"""The Redis relay between this dashboard and its agents.

Neither side accepts inbound connections: both dial out over TLS to Redis (e.g. Upstash)
and exchange JSON messages through two lists per server:

    <prefix>:a:<server id>:cmd   dashboard -> agent   cmd / cancel / cfg
    <prefix>:a:<server id>:in    agent -> dashboard   hb / ack / res

Every agent authenticates as its own ACL user (`sm-<server id>`) allowed only RPUSH, BLPOP,
LTRIM and PING on its own two keys. The dashboard uses the admin connection in REDIS_URL,
which never leaves this machine. Revoking a server is deleting (or disabling) its ACL user.
"""

import json
import logging
import secrets
import time
import urllib.parse
from functools import cache

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

AGENT_COMMANDS = ("+rpush", "+blpop", "+ltrim", "+ping")


class RelayError(Exception):
    """Redis is unreachable or refused an admin operation."""


@cache
def client() -> redis.Redis:
    if not settings.REDIS_URL:
        raise RelayError("REDIS_URL is not configured")
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=settings.RELAY_BLOCK_SECONDS + 15,
        health_check_interval=60,
        retry_on_timeout=False,
    )


# The relay process touches this file every few seconds; the web process reads its age to
# warn when nobody is receiving agent messages. A file next to the SQLite database works
# across both processes (and both containers, which share that volume) with no extra I/O.
STALE_AFTER_SECONDS = 30


def _alive_marker():
    return settings.SQLITE_PATH.parent / ".relay-alive"


def mark_alive() -> None:
    try:
        _alive_marker().touch()
    except OSError as exc:
        logger.warning("Could not write relay liveness marker: %s", exc)


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
    """Create (or reset) the server's ACL user with a fresh password. Returns the agent's
    connection URL. It contains the password, which is never stored by the dashboard."""
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
    return agent_url(user, password)


def agent_url(user: str, password: str) -> str:
    admin = urllib.parse.urlsplit(settings.REDIS_URL)
    host = settings.RELAY_PUBLIC_HOST or admin.hostname
    port = admin.port or 6379
    scheme = "rediss" if admin.scheme == "rediss" else "redis"
    return f"{scheme}://{user}:{urllib.parse.quote(password, safe='')}@{host}:{port}"


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


def command_message(command) -> str:
    """Deterministic JSON for a command, so a queued copy can be found again with LREM."""
    return json.dumps(
        {
            "t": "cmd",
            "id": str(command.pk),
            "action": command.action,
            "args": command.args,
            "timeout": command.timeout_seconds,
            "exp": int(command.deliver_by.timestamp()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def push(server, message: dict) -> None:
    client().rpush(keys(server)[0], json.dumps(message, separators=(",", ":")))


def push_command(command) -> None:
    client().rpush(keys(command.server)[0], command_message(command))


def unqueue_command(command) -> bool:
    """Remove a not-yet-popped command from the agent's mailbox. True if it was still
    there, which proves the agent never received it."""
    return bool(client().lrem(keys(command.server)[0], 1, command_message(command)))


def receive(server_ids, timeout: int) -> tuple[str, dict] | None:
    """Block until any agent sends something. Returns (server id, message) or None."""
    mapping = inbox_keys(server_ids)
    if not mapping:
        time.sleep(min(timeout, 5))
        return None
    reply = client().blpop(list(mapping), timeout=timeout)
    if not reply:
        return None
    key, raw = reply
    try:
        message = json.loads(raw)
    except ValueError:
        logger.warning("Dropping malformed relay message from %s", key)
        return None
    if not isinstance(message, dict):
        return None
    return mapping[key], message
