#!/usr/bin/env python3
"""
Server Monitor agent ("child").

Runs on every monitored server. Neither this agent nor the dashboard accepts inbound
connections: both dial OUT over TLS to a Redis relay (e.g. Upstash) and meet there.

  <prefix>:a:<id>:cmd   dashboard -> agent   jobs, cancels, schedule changes (agent BLPOPs)
  <prefix>:a:<id>:in    agent -> dashboard   heartbeats, acks, streamed output, results

Each agent logs in as its own Redis ACL user that can only push to and pop from its own
two keys, so a compromised server can neither read nor forge another server's traffic.

Security model: the agent's local config is the allowlist. The dashboard can ask for an
action by name; the agent runs it only if this file declares it. Free-form shell is off
unless `[shell] enabled = true`. Commands run as argv lists (no shell) except that one
opt-in action.

Dependencies: Python 3.11+ (tomllib) and psutil. The Redis client is built in.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import platform
import random
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil

VERSION = "2.1.0"
NET_TIMEOUT = 20  # seconds for a single non-blocking Redis round trip
MAX_BLOCK = 120  # longest BLPOP wait (Upstash holds 110 s+); heartbeats go between waits
MAX_CHUNK = 60 * 1024  # main accepts 64 KiB per progress message
FLUSH_EVERY = 3.0  # seconds between streamed output messages
INBOX_KEEP = 5000  # newest messages kept if the dashboard is offline for a long time
MAX_TAIL_LINES = 2000
VERSION_CACHE_SECONDS = 300
PSEUDO_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "devfs", "autofs"}

log = logging.getLogger("server-monitor-agent")


# --------------------------------------------------------------------------- config


class ConfigError(Exception):
    pass


@dataclass
class ServiceSpec:
    name: str
    kind: str  # systemd | docker | pm2 | process | http
    unit: str = ""  # systemd unit / container / pm2 app / process match / URL
    restartable: bool = False
    restart_command: list[str] = field(default_factory=list)
    version_command: list[str] = field(default_factory=list)
    version_file: str = ""


@dataclass
class ActionSpec:
    name: str
    command: list[str]
    description: str = ""
    timeout: int = 300
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    redis_url: str
    server_id: str
    # Per-server HMAC key (from the connection string). None = legacy, unsigned traffic.
    signing_key: bytes | None = None
    key_prefix: str = "sm"
    verify_tls: bool = True
    ca_file: str | None = None
    report_interval: int = 120
    max_concurrent_commands: int = 2
    disk_mounts: list[str] = field(default_factory=list)
    services: dict[str, ServiceSpec] = field(default_factory=dict)
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    logs: dict[str, str] = field(default_factory=dict)
    shell_enabled: bool = False
    shell_timeout: int = 120
    use_sudo_for_restart: bool = True


BUILTIN_NAMES = {"tail_log", "restart_service", "shell"}
SERVICE_KINDS = {"systemd", "docker", "pm2", "process", "http"}


def _argv(value, where: str) -> list[str]:
    if isinstance(value, str):
        # A string is split on whitespace only; anything needing quoting must be a list.
        value = value.split()
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: command must be a non-empty list of strings")
    return value


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    main = raw.get("main", {})
    redis_url = os.environ.get("SM_REDIS") or main.get("redis_url", "")
    if not redis_url:
        raise ConfigError("[main] redis_url is required (the connection string from the dashboard)")
    parsed = urllib.parse.urlsplit(redis_url)
    if parsed.scheme not in ("rediss", "redis") or not parsed.hostname:
        raise ConfigError("main.redis_url must look like rediss://user:token@host:6379")
    if parsed.scheme == "redis" and not main.get("allow_insecure", False):
        # The credential and every job would cross the network in clear text.
        raise ConfigError(
            "main.redis_url must use rediss:// (TLS); allow_insecure = true for local dev"
        )
    user = urllib.parse.unquote(parsed.username or "")
    server_id = str(main.get("server_id") or (user[3:] if user.startswith("sm-") else ""))
    if not server_id:
        raise ConfigError(
            "cannot tell this server's id: user must be sm-<id> or set main.server_id"
        )
    key_hex = (urllib.parse.parse_qs(parsed.query).get("key") or [main.get("signing_key", "")])[0]
    try:
        signing_key = bytes.fromhex(key_hex) if key_hex else None
    except ValueError:
        raise ConfigError("the signing key in redis_url is not valid hex") from None
    if signing_key is not None and len(signing_key) != 32:
        raise ConfigError("the signing key in redis_url must be 32 bytes")

    agent = raw.get("agent", {})
    cfg = Config(
        redis_url=redis_url,
        server_id=server_id,
        signing_key=signing_key,
        key_prefix=str(main.get("key_prefix", "sm")),
        verify_tls=bool(main.get("verify_tls", True)),
        ca_file=main.get("ca_file"),
        report_interval=max(10, int(agent.get("report_interval", 120))),
        max_concurrent_commands=max(1, int(agent.get("max_concurrent_commands", 2))),
        disk_mounts=list(agent.get("disk_mounts", [])),
        shell_enabled=bool(raw.get("shell", {}).get("enabled", False)),
        shell_timeout=int(raw.get("shell", {}).get("timeout", 120)),
        use_sudo_for_restart=bool(agent.get("use_sudo_for_restart", True)),
    )

    for name, svc in raw.get("services", {}).items():
        kind = svc.get("kind", "systemd")
        if kind not in SERVICE_KINDS:
            raise ConfigError(f"services.{name}: kind must be one of {sorted(SERVICE_KINDS)}")
        cfg.services[name] = ServiceSpec(
            name=name,
            kind=kind,
            unit=svc.get("unit") or svc.get("url") or name,
            restartable=bool(svc.get("restartable", False)),
            restart_command=_argv(svc["restart_command"], f"services.{name}")
            if svc.get("restart_command")
            else [],
            version_command=_argv(svc["version_command"], f"services.{name}")
            if svc.get("version_command")
            else [],
            version_file=svc.get("version_file", ""),
        )

    for name, act in raw.get("actions", {}).items():
        if name in BUILTIN_NAMES:
            raise ConfigError(f"actions.{name}: name is reserved for a built-in action")
        cfg.actions[name] = ActionSpec(
            name=name,
            command=_argv(act.get("command"), f"actions.{name}"),
            description=act.get("description", ""),
            timeout=int(act.get("timeout", 300)),
            cwd=act.get("cwd"),
            env={str(k): str(v) for k, v in act.get("env", {}).items()},
        )

    for name, file_path in raw.get("logs", {}).items():
        cfg.logs[name] = str(file_path)
    return cfg


# --------------------------------------------------------------------------- collection


def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 126, str(exc)


def _primary_address() -> str:
    """The IP this host uses for outbound traffic (no packet is sent: UDP connect only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1: routable shape, never answered
            return sock.getsockname()[0]
    except OSError:
        return ""


class Collector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._last_net: tuple[float, int, int] | None = None
        self._versions: dict[str, tuple[float, str]] = {}
        psutil.cpu_percent(interval=None)  # prime: the first reading is always 0.0

    def host(self) -> dict:
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "cpu_count": psutil.cpu_count() or 0,
            "memory_total": psutil.virtual_memory().total,
            "uptime_seconds": int(time.time() - psutil.boot_time()),
            "address": _primary_address(),
        }

    def disks(self) -> list[dict]:
        mounts = self.cfg.disk_mounts or [
            p.mountpoint
            for p in psutil.disk_partitions(all=False)
            if p.fstype.lower() not in PSEUDO_FS
            and not p.mountpoint.startswith(("/snap", "/System/Volumes/"))
        ]
        out, seen = [], set()
        for mount in mounts or ["/"]:
            try:
                usage = psutil.disk_usage(mount)
            except OSError:
                continue
            key = (usage.total, usage.used)
            if key in seen:  # bind mounts of the same device
                continue
            seen.add(key)
            out.append(
                {"mount": mount, "total": usage.total, "used": usage.used, "percent": usage.percent}
            )
        return out[:32]

    def network(self) -> tuple[float | None, float | None]:
        now = time.monotonic()
        counters = psutil.net_io_counters()
        rx, tx = counters.bytes_recv, counters.bytes_sent
        prev, self._last_net = self._last_net, (now, rx, tx)
        if prev is None or now <= prev[0]:
            return None, None
        dt = now - prev[0]
        return max(0.0, (rx - prev[1]) / dt), max(0.0, (tx - prev[2]) / dt)

    def metrics(self, disks: list[dict]) -> dict:
        rx, tx = self.network()
        try:
            load_1 = os.getloadavg()[0]
        except OSError:
            load_1 = None
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            # The fullest disk is the one that takes the server down.
            "disk_percent": max((d["percent"] for d in disks), default=0.0),
            "load_1": load_1,
            "net_rx_bps": rx,
            "net_tx_bps": tx,
        }

    def _version(self, svc: ServiceSpec) -> str:
        cached = self._versions.get(svc.name)
        if cached and time.monotonic() - cached[0] < VERSION_CACHE_SECONDS:
            return cached[1]
        value = ""
        if svc.version_file:
            try:
                value = Path(svc.version_file).read_text(errors="replace").strip().splitlines()[0]
            except (OSError, IndexError):
                value = ""
        elif svc.version_command:
            _, text = _run(svc.version_command)
            value = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        self._versions[svc.name] = (time.monotonic(), value[:100])
        return value[:100]

    def services(self) -> list[dict]:
        pm2 = None
        out = []
        for svc in self.cfg.services.values():
            state, status = "unknown", ""
            if svc.kind == "systemd":
                code, text = _run(["systemctl", "is-active", svc.unit])
                status = text.strip().splitlines()[0] if text.strip() else ""
                state = "up" if status == "active" else ("unknown" if code == 127 else "down")
            elif svc.kind == "docker":
                code, text = _run(["docker", "inspect", "--format", "{{.State.Status}}", svc.unit])
                status = text.strip()[:60]
                state = (
                    "up" if status == "running" else ("unknown" if code in (126, 127) else "down")
                )
            elif svc.kind == "pm2":
                if pm2 is None:
                    code, text = _run(["pm2", "jlist"], timeout=10)
                    try:
                        pm2 = (
                            {p["name"]: p["pm2_env"]["status"] for p in json.loads(text)}
                            if code == 0
                            else {}
                        )
                    except (ValueError, KeyError, TypeError):
                        pm2 = {}
                status = pm2.get(svc.unit, "not found")
                state = "up" if status == "online" else "down"
            elif svc.kind == "process":
                running = any(
                    svc.unit in (p.info.get("name") or "")
                    or svc.unit in " ".join(p.info.get("cmdline") or [])
                    for p in psutil.process_iter(["name", "cmdline"])
                )
                state, status = ("up", "running") if running else ("down", "not running")
            elif svc.kind == "http":
                try:
                    req = urllib.request.Request(svc.unit, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        status = f"HTTP {resp.status}"
                        state = "up" if resp.status < 400 else "down"
                except urllib.error.HTTPError as exc:
                    state, status = "down", f"HTTP {exc.code}"
                except (OSError, ValueError) as exc:
                    state, status = "down", type(exc).__name__
            out.append(
                {
                    "name": svc.name,
                    "kind": svc.kind,
                    "state": state,
                    "status": status,
                    "version": self._version(svc),
                    "restartable": svc.restartable,
                }
            )
        return out

    def capabilities(self) -> dict:
        return {
            "actions": [
                {"name": a.name, "description": a.description, "timeout": a.timeout}
                for a in self.cfg.actions.values()
            ],
            "logs": sorted(self.cfg.logs),
            "shell": self.cfg.shell_enabled,
        }

    def report(self) -> dict:
        disks = self.disks()
        return {
            "agent_version": VERSION,
            "host": self.host(),
            "metrics": self.metrics(disks),
            "disks": disks,
            "services": self.services(),
            "capabilities": self.capabilities(),
        }


# --------------------------------------------------------------------------- execution


class Rejected(Exception):
    """The command is not allowed by this agent's config."""


def tail_file(path: str, lines: int) -> str:
    """Last `lines` lines of a file, reading backwards in blocks (logs can be gigabytes)."""
    lines = max(1, min(int(lines), MAX_TAIL_LINES))
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos, chunks, newlines = fh.tell(), [], 0
        while pos > 0 and newlines <= lines:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step)
            chunks.append(block)
            newlines += block.count(b"\n")
    data = b"".join(reversed(chunks))
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:]) + "\n"


@dataclass
class Plan:
    argv: list[str] | None = None
    timeout: int = 300
    cwd: str | None = None
    env: dict[str, str] | None = None
    inline_output: str | None = None  # result produced without a subprocess


def plan_command(cfg: Config, action: str, args: dict) -> Plan:
    """Map a requested action to what will actually run — or refuse. This function is
    the security boundary: nothing reaches subprocess without passing through it."""
    args = args if isinstance(args, dict) else {}
    if action == "tail_log":
        name = str(args.get("name", ""))
        if name not in cfg.logs:
            raise Rejected(f"log {name!r} is not declared in [logs]")
        try:
            return Plan(inline_output=tail_file(cfg.logs[name], args.get("lines", 200)))
        except OSError as exc:
            raise Rejected(f"cannot read {cfg.logs[name]}: {exc.strerror}") from exc
    if action == "restart_service":
        name = str(args.get("name", ""))
        svc = cfg.services.get(name)
        if svc is None or not svc.restartable:
            raise Rejected(f"service {name!r} is not declared restartable")
        if svc.restart_command:
            argv = svc.restart_command
        elif svc.kind == "systemd":
            argv = ["systemctl", "restart", svc.unit]
            if cfg.use_sudo_for_restart and os.geteuid() != 0:
                argv = ["sudo", "-n", *argv]
        elif svc.kind == "docker":
            argv = ["docker", "restart", svc.unit]
        elif svc.kind == "pm2":
            argv = ["pm2", "restart", svc.unit]
        else:
            raise Rejected(f"service {name!r} ({svc.kind}) needs an explicit restart_command")
        return Plan(argv=argv, timeout=120)
    if action == "shell":
        if not cfg.shell_enabled:
            raise Rejected("free-form shell is disabled on this server ([shell] enabled = false)")
        command = str(args.get("command", ""))
        if not command.strip():
            raise Rejected("empty shell command")
        return Plan(argv=["/bin/sh", "-c", command], timeout=cfg.shell_timeout)
    spec = cfg.actions.get(action)
    if spec is None:
        raise Rejected(f"action {action!r} is not declared in this agent's config")
    env = {**os.environ, **spec.env} if spec.env else None
    return Plan(argv=spec.command, timeout=spec.timeout, cwd=spec.cwd, env=env)


def _canonical(message: dict) -> bytes:
    body = {k: v for k, v in message.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(message: dict, key: bytes) -> dict:
    return {**message, "sig": hmac.new(key, _canonical(message), hashlib.sha256).hexdigest()}


def verify(message: dict, key: bytes) -> bool:
    """Only the dashboard (and this agent) know the key; Redis merely carries messages, so
    anything unsigned or mis-signed was not written by the dashboard."""
    sig = message.get("sig")
    if not isinstance(sig, str):
        return False
    return hmac.compare_digest(sig, hmac.new(key, _canonical(message), hashlib.sha256).hexdigest())


class SeenCommands:
    """IDs of commands already run, kept on disk (systemd StateDirectory) so a captured,
    validly signed command can't be replayed to this agent, even across a restart."""

    def __init__(self, path: str | None, keep: int = 1000):
        self.path = Path(path) / "seen-commands.json" if path else None
        self.ids: deque[str] = deque(maxlen=keep)
        if self.path and self.path.exists():
            try:
                self.ids.extend(json.loads(self.path.read_text())[-keep:])
            except (OSError, ValueError):
                log.warning("Could not read %s; starting with an empty replay list", self.path)

    def __contains__(self, cid: str) -> bool:
        return cid in self.ids

    def add(self, cid: str) -> None:
        self.ids.append(cid)
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(list(self.ids)))
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("Could not save replay list: %s", exc)


class RedisError(Exception):
    """An error reply from Redis (wrong credentials, ACL denial, ...)."""


class Redis:
    """Just enough of RESP2 for the agent: AUTH plus a few list commands, over TLS.
    One instance is one connection; callers serialise access with their own lock."""

    def __init__(self, url: str, verify_tls: bool = True, ca_file: str | None = None):
        u = urllib.parse.urlsplit(url)
        self.host, self.port = u.hostname, u.port or 6379
        self.username = urllib.parse.unquote(u.username or "")
        self.password = urllib.parse.unquote(u.password or "")
        self.tls = u.scheme == "rediss"
        self.ctx = None
        if self.tls:
            self.ctx = ssl.create_default_context(cafile=ca_file)
            if not verify_tls:  # explicit opt-out in agent.toml
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
        self.sock = None
        self.reader = None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = self.reader = None

    def interrupt(self) -> None:
        """Wake a thread blocked in BLPOP on this connection (called from a signal handler)."""
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=NET_TIMEOUT)
        if self.ctx is not None:
            sock = self.ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock, self.reader = sock, sock.makefile("rb")
        if self.password:
            auth = (
                ["AUTH", self.username, self.password] if self.username else ["AUTH", self.password]
            )
            self._send(auth)
            self._read()

    def _send(self, args) -> None:
        out = [b"*%d\r\n" % len(args)]
        for arg in args:
            data = arg if isinstance(arg, bytes) else str(arg).encode()
            out.append(b"$%d\r\n%s\r\n" % (len(data), data))
        self.sock.sendall(b"".join(out))

    def _read(self):
        line = self.reader.readline()
        if not line:
            raise ConnectionError("Redis closed the connection")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode(errors="replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.reader.read(n + 2)
            return data[:-2].decode("utf-8", errors="replace")
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise ConnectionError(f"unexpected Redis reply {line[:20]!r}")

    def call(self, *args, block: float = 0):
        """Run one command. `block` is how long the server may legitimately stay silent."""
        if self.sock is None:
            self._connect()
        try:
            self.sock.settimeout(NET_TIMEOUT + block)
            self._send(args)
            return self._read()
        except (OSError, ConnectionError):
            self.close()  # a half-read reply poisons the stream; reconnect next time
            raise


class Relay:
    """The agent's two mailboxes. Writes (heartbeats, output) and the blocking read for
    jobs use separate connections so streaming output never waits behind a BLPOP."""

    def __init__(self, cfg: Config):
        self.key = cfg.signing_key
        base = f"{cfg.key_prefix}:a:{cfg.server_id}"
        self.inbox, self.outbox = f"{base}:cmd", f"{base}:in"
        self.writer = Redis(cfg.redis_url, cfg.verify_tls, cfg.ca_file)
        self.reader = Redis(cfg.redis_url, cfg.verify_tls, cfg.ca_file)
        self.lock = threading.Lock()
        self.sent = 0

    def send(self, message: dict) -> None:
        if self.key:
            message = sign(message, self.key)
        payload = json.dumps(message, sort_keys=True, separators=(",", ":"))
        with self.lock:
            self.writer.call("RPUSH", self.outbox, payload)
            self.sent += 1
            if self.sent % 50 == 0:
                # Bound the mailbox if the dashboard is down for days: keep the newest.
                self.writer.call("LTRIM", self.outbox, -INBOX_KEEP, -1)

    def receive(self, timeout: float) -> dict | None:
        wait = max(1, int(timeout))
        reply = self.reader.call("BLPOP", self.inbox, wait, block=wait)
        if not reply:
            return None
        try:
            message = json.loads(reply[1])
        except ValueError:
            log.warning("Ignoring malformed message in %s", self.inbox)
            return None
        return message if isinstance(message, dict) else None

    def requeue(self, message: dict) -> None:
        """Put back a job popped while shutting down. Same canonical JSON as the dashboard
        writes, so it can still find (and cancel) it with LREM."""
        payload = json.dumps(message, sort_keys=True, separators=(",", ":"))
        with self.lock:
            self.writer.call("RPUSH", self.inbox, payload)

    def close(self) -> None:
        self.writer.close()
        self.reader.close()


class Executor:
    def __init__(self, cfg: Config, relay: Relay):
        self.cfg, self.relay = cfg, relay
        self.pool = ThreadPoolExecutor(
            max_workers=cfg.max_concurrent_commands, thread_name_prefix="cmd"
        )
        self._seen = SeenCommands(os.environ.get("STATE_DIRECTORY"))
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, command: dict) -> None:
        cid = command.get("id")
        with self._lock:
            # Each job is popped once; this guards against a message pushed twice.
            if not cid or cid in self._seen:
                log.warning("Ignoring command %s: already run (replay?)", cid)
                return
            self._seen.add(cid)
        self.pool.submit(self._run_safely, command)

    def cancel(self, cid: str) -> None:
        with self._lock:
            self._cancelled.add(cid)

    def _is_cancelled(self, cid: str) -> bool:
        with self._lock:
            return cid in self._cancelled

    def _post(
        self,
        cid: str,
        status: str,
        output: str = "",
        exit_code: int | None = None,
        retries: int = 0,
    ) -> None:
        body = {
            "t": "res",
            "id": cid,
            "status": status,
            "output": output[-MAX_CHUNK:],
            "exit_code": exit_code,
        }
        for attempt in range(retries + 1):
            try:
                self.relay.send(body)
                return
            except (OSError, ConnectionError, RedisError) as exc:
                if attempt == retries:
                    log.warning("Could not report command %s: %s", cid, exc)
                    return
                time.sleep(min(30, 2**attempt) + random.random())

    def _run_safely(self, command: dict) -> None:
        cid, action = command["id"], command.get("action", "")
        try:
            self._run(cid, action, command.get("args") or {})
        except Exception as exc:  # never let a bug kill the worker thread silently
            log.exception("Command %s (%s) crashed", cid, action)
            self._post(cid, "failed", f"agent error: {exc}\n", None, retries=3)

    def _run(self, cid: str, action: str, args: dict) -> None:
        log.info("Command %s: %s %s", cid, action, json.dumps(args)[:200])
        try:
            plan = plan_command(self.cfg, action, args)
        except Rejected as exc:
            log.warning("Rejected %s: %s", action, exc)
            self._post(cid, "failed", f"rejected by agent: {exc}\n", None, retries=3)
            return
        if plan.inline_output is not None:
            self._post(cid, "succeeded", plan.inline_output, 0, retries=3)
            return

        try:
            proc = subprocess.Popen(
                plan.argv,
                cwd=plan.cwd,
                env=plan.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group so a timeout kills children too
            )
        except OSError as exc:
            self._post(cid, "failed", f"cannot start {plan.argv[0]}: {exc}\n", 127, retries=3)
            return

        buffer: list[bytes] = []
        buf_lock = threading.Lock()

        def reader():
            for line in iter(proc.stdout.readline, b""):
                with buf_lock:
                    buffer.append(line)
            proc.stdout.close()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        deadline = time.monotonic() + plan.timeout
        timed_out = cancelled = False
        last_flush = time.monotonic()

        def drain() -> str:
            with buf_lock:
                data = b"".join(buffer)
                buffer.clear()
            return data.decode("utf-8", errors="replace")

        while proc.poll() is None:
            if time.monotonic() > deadline:
                timed_out = True
                self._kill(proc)
                break
            if self._is_cancelled(cid):
                cancelled = True
                self._kill(proc)
                break
            time.sleep(0.2)
            if time.monotonic() - last_flush >= FLUSH_EVERY:
                last_flush = time.monotonic()
                if chunk := drain():
                    self._post(cid, "running", chunk)
        proc.wait()
        t.join(timeout=5)
        tail = drain()
        if timed_out:
            tail += f"\n[agent] killed after {plan.timeout}s timeout\n"
        if cancelled:
            tail += "\n[agent] stopped: the dashboard cancelled this command\n"
        code = proc.returncode
        status = "succeeded" if code == 0 and not (timed_out or cancelled) else "failed"
        # Large final output goes up in chunks so nothing is cut at the 64 KiB limit.
        while len(tail) > MAX_CHUNK:
            self._post(cid, "running", tail[:MAX_CHUNK], retries=2)
            tail = tail[MAX_CHUNK:]
        self._post(cid, status, tail, code, retries=5)
        log.info("Command %s finished: %s (exit %s)", cid, status, code)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


# --------------------------------------------------------------------------- main loop


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.relay = Relay(cfg)
        self.collector = Collector(cfg)
        self.executor = Executor(cfg, self.relay)
        self.stop = threading.Event()
        self.report_interval = cfg.report_interval
        self.failures = 0

    def request_stop(self) -> None:
        self.stop.set()
        self.relay.reader.interrupt()  # don't sit out a 2-minute BLPOP on shutdown

    def _backoff(self) -> float:
        self.failures += 1
        return min(300.0, 2 ** min(self.failures, 8)) * (0.5 + random.random() / 2)

    def heartbeat(self) -> bool:
        try:
            report = self.collector.report()
            self.relay.send({"t": "hb", "interval": self.report_interval, "report": report})
            self.failures = 0
            return True
        except RedisError as exc:
            if "WRONGPASS" in str(exc) or "NOPERM" in str(exc):
                log.error(
                    "The relay rejected this agent's credentials (%s). Was the token rotated "
                    "or the server disabled? Install the new ones: sudo smctl connect ...",
                    exc,
                )
            else:
                log.warning("Heartbeat failed: %s", exc)
        except (OSError, ConnectionError, ValueError) as exc:
            log.warning("Heartbeat failed: %s", exc)
        return False

    def handle(self, message: dict) -> None:
        kind = message.get("t")
        if self.cfg.signing_key and not verify(message, self.cfg.signing_key):
            # Written by someone with Redis access, not by the dashboard: never act on it.
            log.error("Rejected an unsigned or forged %r message from the relay", kind)
            return
        if kind == "cmd":
            if float(message.get("exp") or 0) < time.time():
                # Queued while this agent was offline and nobody is waiting any more.
                log.info(
                    "Skipping expired command %s (%s)", message.get("id"), message.get("action")
                )
                return
            try:
                self.relay.send({"t": "ack", "id": message.get("id")})
            except (OSError, ConnectionError, RedisError) as exc:
                log.warning("Could not acknowledge command: %s", exc)
            self.executor.submit(message)
        elif kind == "cancel":
            self.executor.cancel(str(message.get("id")))
        elif kind == "cfg":
            # The dashboard owns the schedule: changing it there retunes the agent.
            self.report_interval = max(
                10, int(message.get("report_interval") or self.report_interval)
            )
            log.info("Report interval set to %ss by the dashboard", self.report_interval)

    def run(self) -> None:
        log.info(
            "Agent %s for server %s via %s",
            VERSION,
            self.cfg.server_id,
            urllib.parse.urlsplit(self.cfg.redis_url).hostname,
        )
        if not self.cfg.signing_key:
            log.warning(
                "Running WITHOUT message signing (legacy credentials). Rotate this server's "
                "credentials in the dashboard, then: sudo smctl connect"
            )
        next_report = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_report:
                ok = self.heartbeat()
                next_report = now + (self.report_interval if ok else self._backoff())
            # Block on the job mailbox until the next heartbeat is due: jobs start the
            # instant they're queued, and an idle agent costs ~2 Redis commands per interval.
            wait = min(MAX_BLOCK, max(1.0, next_report - time.monotonic()))
            try:
                message = self.relay.receive(wait)
            except (OSError, ConnectionError, RedisError) as exc:
                if self.stop.is_set():
                    break  # interrupted on purpose
                log.warning("Relay unavailable: %s", exc)
                self.stop.wait(min(30.0, self._backoff()))
                continue
            if message and self.stop.is_set() and message.get("t") == "cmd":
                # Popped in the instant we were told to stop: hand it back unrun.
                try:
                    self.relay.requeue(message)
                except (OSError, ConnectionError, RedisError) as exc:
                    log.error(
                        "Could not return command %s to the queue: %s", message.get("id"), exc
                    )
                break
            if message:
                self.handle(message)
        log.info("Stopping; waiting for running commands")
        self.executor.shutdown()
        self.relay.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Server Monitor agent")
    parser.add_argument("-c", "--config", default="/etc/server-monitor-agent/agent.toml")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    parser.add_argument(
        "--print", action="store_true", dest="print_only", help="Print the report JSON and exit"
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate the config and exit (used by smctl)"
    )
    parser.add_argument(
        "--ping", action="store_true", help="Log in to the relay and exit (used by the installer)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    opts = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = load_config(opts.config)
    except (OSError, ConfigError, tomllib.TOMLDecodeError) as exc:
        log.error("Config error in %s: %s", opts.config, exc)
        return 2
    if opts.check:
        print(f"ok: {len(cfg.services)} services, {len(cfg.logs)} logs, {len(cfg.actions)} actions")
        return 0
    if opts.ping:
        try:
            Redis(cfg.redis_url, cfg.verify_tls, cfg.ca_file).call("PING")
        except RedisError as exc:
            print(f"relay refused the credentials: {exc}", file=sys.stderr)
            return 3
        except (OSError, ConnectionError) as exc:
            print(f"cannot reach the relay: {exc}", file=sys.stderr)
            return 4
        print(f"ok: logged in to {urllib.parse.urlsplit(cfg.redis_url).hostname}")
        return 0
    if opts.print_only:
        collector = Collector(cfg)
        time.sleep(0.5)  # let cpu_percent measure something
        print(json.dumps(collector.report(), indent=2))
        return 0
    agent = Agent(cfg)
    if opts.once:
        ok = agent.heartbeat()
        agent.executor.shutdown()
        agent.relay.close()
        return 0 if ok else 1
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: agent.request_stop())
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
