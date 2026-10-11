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
import glob
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import platform
import random
import re
import shutil
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

VERSION = "2.2.0"
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
    inventory: bool = True  # processes, temperatures, containers, GPUs in each heartbeat
    # Let the dashboard ask root (server-monitor-upgrade.path) to install a release tag.
    remote_upgrade: bool = False
    # Run HTTP/TCP checks the dashboard assigns (agents as uptime probes; opt-in).
    probes: bool = False
    docker_names: bool = False  # `smctl docker enable` gives the agent docker access
    report_interval: int = 120
    max_concurrent_commands: int = 2
    disk_mounts: list[str] = field(default_factory=list)
    services: dict[str, ServiceSpec] = field(default_factory=dict)
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    logs: dict[str, str] = field(default_factory=dict)
    shell_enabled: bool = False
    shell_timeout: int = 120
    use_sudo_for_restart: bool = True


BUILTIN_NAMES = {"tail_log", "tail_follow", "restart_service", "shell", "_upgrade"}
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
        inventory=bool(agent.get("inventory", True)),
        remote_upgrade=bool(agent.get("remote_upgrade", False)),
        probes=bool(raw.get("probes", {}).get("enabled", False)),
        docker_names=bool(raw.get("docker", {}).get("names", False)),
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

    # ---------------------------------------------------------------- inventory
    def processes(self) -> dict:
        """Top 10 by CPU and by memory. psutil keeps Process objects between calls, so
        cpu_percent is measured over the time since the previous heartbeat."""
        rows = []
        for p in psutil.process_iter(["pid", "name", "username", "cmdline", "memory_info"]):
            try:
                cpu = p.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                continue
            info = p.info
            rss = info["memory_info"].rss if info.get("memory_info") else 0
            rows.append({"pid": info["pid"], "name": (info.get("name") or "")[:60],
                         "user": (info.get("username") or "")[:32], "cpu": round(cpu, 1), "rss": rss,
                         "cmd": " ".join(info.get("cmdline") or [])[:120]})  # fmt: skip
        by_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:10]
        by_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:10]
        return {"cpu": by_cpu, "memory": by_mem, "count": len(rows)}

    def temperatures(self) -> list[dict]:
        read = getattr(psutil, "sensors_temperatures", None)
        if read is None:
            return []
        try:
            sensors = read() or {}
        except (OSError, RuntimeError):
            return []
        out = []
        for chip, entries in sensors.items():
            for e in entries:
                if e.current is None or e.current <= 0:
                    continue
                out.append({"sensor": f"{chip} {e.label}".strip()[:60], "current": round(e.current, 1),
                            "high": e.high or None, "critical": e.critical or None})  # fmt: skip
        return sorted(out, key=lambda t: t["current"], reverse=True)[:16]

    def gpus(self) -> list[dict]:
        if not shutil.which("nvidia-smi"):
            return []
        code, text = _run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                           "--format=csv,noheader,nounits"], timeout=10)  # fmt: skip
        if code != 0:
            return []
        out = []
        for line in text.strip().splitlines()[:8]:
            parts = [x.strip() for x in line.split(",")]
            if len(parts) != 5:
                continue
            try:
                out.append({"name": parts[0][:60], "util": float(parts[1]), "mem_used": float(parts[2]) * 2**20,
                            "mem_total": float(parts[3]) * 2**20, "temp": float(parts[4])})  # fmt: skip
            except ValueError:
                continue
        return out

    def containers(self) -> list[dict]:
        """Running containers from cgroup v2 files (no docker group needed); names and
        stopped containers from `docker ps` when the agent may use Docker."""
        now = time.monotonic()
        prev, self._last_cgroups = getattr(self, "_last_cgroups", {}), {}
        found = {}
        for pattern in CGROUP_GLOBS:
            for path in glob.glob(pattern):
                m = CONTAINER_ID_RE.search(path)
                if not m:
                    continue
                cid = m.group(1)
                try:
                    usage = int(next(ln.split()[1] for ln in Path(path, "cpu.stat").read_text().splitlines()
                                     if ln.startswith("usage_usec")))  # fmt: skip
                    memory = int(Path(path, "memory.current").read_text().strip())
                except (OSError, StopIteration, ValueError, IndexError):
                    continue
                cpu = None
                if cid in prev and now > prev[cid][0]:
                    cpu = round(
                        max(0.0, (usage - prev[cid][1]) / ((now - prev[cid][0]) * 1e6) * 100), 1
                    )
                self._last_cgroups[cid] = (now, usage)
                found[cid[:12]] = {"id": cid[:12], "name": cid[:12], "image": "", "state": "running",
                                   "cpu": cpu, "memory": memory}  # fmt: skip
        if self.cfg.docker_names and shutil.which("docker"):
            code, text = _run(["docker", "ps", "-a", "--no-trunc", "--format",
                               "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}"], timeout=10)  # fmt: skip
            if code == 0:
                for line in text.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) != 4:
                        continue
                    row = found.setdefault(
                        parts[0][:12], {"id": parts[0][:12], "cpu": None, "memory": None}
                    )
                    row.update(name=parts[1][:80], image=parts[2][:120], state=parts[3][:20])
        return sorted(found.values(), key=lambda c: c["name"])[:50]

    def inventory(self) -> dict:
        if not self.cfg.inventory:
            return {}
        return {"containers": self.containers(), "processes": self.processes(),
                "temps": self.temperatures(), "gpus": self.gpus()}  # fmt: skip

    def capabilities(self) -> dict:
        return {
            "features": FEATURES
            + (["remote_upgrade"] if self.cfg.remote_upgrade else [])
            + (["probes"] if self.cfg.probes else []),
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
            "inventory": self.inventory(),
        }


# --------------------------------------------------------------------------- execution


class Rejected(Exception):
    """The command is not allowed by this agent's config."""


def _follow_end(stopped: bool, seconds: int) -> str:
    return "stopped from the dashboard" if stopped else f"stopped following after {seconds} s"


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
    # Handed to another process that reports the final result itself (remote upgrades).
    detached_output: str | None = None
    # Live log follow: a declared file to follow, or a journalctl -f subprocess (follow=True).
    # Reaching the timeout or being stopped is the normal end, not a failure.
    follow_file: str | None = None
    follow: bool = False
    line_filter: str = ""


UPGRADE_REF_RE = re.compile(r"^v\d{1,4}\.\d{1,4}\.\d{1,4}$")  # release tags only
SUM_RE = re.compile(r"^[0-9a-f]{64}$")
COMMAND_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
UPGRADE_FILES = ("install", "agent", "smctl", "unit", "upath", "uservice")
UPGRADE_REQUEST = "upgrade-request.json"


def _state_dir() -> Path:
    return Path(os.environ.get("STATE_DIRECTORY") or "/var/lib/server-monitor-agent")


def request_upgrade(cfg: Config, command_id: str, args: dict) -> str:
    """Write the request that root's server-monitor-upgrade.path acts on. The agent never
    installs anything itself; `smctl upgrade --from-request` (root) re-validates all of
    this and only installs a release tag of the repository recorded at install."""
    if not cfg.remote_upgrade:
        raise Rejected("remote upgrades are off on this server (sudo smctl remote-upgrade on)")
    ref, sums = args.get("ref"), args.get("sums")
    if not isinstance(ref, str) or not UPGRADE_REF_RE.match(ref):
        raise Rejected("upgrades must name a release tag like v2.3.0")
    if not isinstance(sums, dict) or set(sums) != set(UPGRADE_FILES):
        raise Rejected(f"upgrades need checksums for {', '.join(UPGRADE_FILES)}")
    if not all(isinstance(v, str) and SUM_RE.match(v) for v in sums.values()):
        raise Rejected("checksums must be SHA-256 hex digests")
    if not COMMAND_ID_RE.match(command_id or ""):
        raise Rejected("bad command id")
    target = _state_dir() / UPGRADE_REQUEST
    tmp = target.with_suffix(".tmp")
    body = {"id": command_id, "ref": ref, "sums": sums, "at": int(time.time())}
    try:
        tmp.write_text(json.dumps(body, sort_keys=True))
        os.replace(tmp, target)  # the path unit sees one complete file, never a partial one
    except OSError as exc:
        raise Rejected(f"cannot write the upgrade request: {exc.strerror}") from exc
    return f"Upgrade to {ref} requested. Waiting for root (server-monitor-upgrade) to install it…\n"


FOLLOW_MAX_SECONDS = 600


def plan_follow(cfg: Config, args: dict) -> Plan:
    """Follow a declared log file, or the journal of a declared systemd service."""
    try:
        seconds = max(10, min(FOLLOW_MAX_SECONDS, int(args.get("seconds") or FOLLOW_MAX_SECONDS)))
    except (TypeError, ValueError):
        raise Rejected("seconds must be a number") from None
    line_filter = str(args.get("filter") or "")[:200]
    name = str(args.get("name", ""))
    if args.get("source") == "journal":
        svc = cfg.services.get(name)
        if svc is None or svc.kind != "systemd":
            raise Rejected(f"{name!r} is not a declared systemd service")
        argv = ["journalctl", f"--unit={svc.unit}", "--follow", "--lines=20", "--output=short-iso",
                "--no-pager", "--quiet"]  # fmt: skip
        return Plan(argv=argv, timeout=seconds, follow=True, line_filter=line_filter)
    if name not in cfg.logs:
        raise Rejected(f"log {name!r} is not declared in [logs]")
    if not os.access(cfg.logs[name], os.R_OK):
        raise Rejected(f"cannot read {cfg.logs[name]}")
    return Plan(follow_file=cfg.logs[name], timeout=seconds, follow=True, line_filter=line_filter)


def plan_command(cfg: Config, action: str, args: dict, command_id: str = "") -> Plan:
    """Map a requested action to what will actually run — or refuse. This function is
    the security boundary: nothing reaches subprocess without passing through it."""
    args = args if isinstance(args, dict) else {}
    if action == "tail_follow":
        return plan_follow(cfg, args)
    if action == "_upgrade":
        return Plan(detached_output=request_upgrade(cfg, command_id, args))
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
            plan = plan_command(self.cfg, action, args, cid)
        except Rejected as exc:
            log.warning("Rejected %s: %s", action, exc)
            self._post(cid, "failed", f"rejected by agent: {exc}\n", None, retries=3)
            return
        if plan.detached_output is not None:
            self._post(cid, "running", plan.detached_output, retries=3)
            return
        if plan.follow_file is not None:
            self._follow_file(cid, plan)
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

        wanted = plan.line_filter.encode()

        def reader():
            for line in iter(proc.stdout.readline, b""):
                if wanted and wanted not in line:
                    continue
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
        code = proc.returncode
        if plan.follow and (timed_out or cancelled):
            tail += f"\n[agent] {_follow_end(cancelled, plan.timeout)}\n"
            status, code = "succeeded", 0
        else:
            if timed_out:
                tail += f"\n[agent] killed after {plan.timeout}s timeout\n"
            if cancelled:
                tail += "\n[agent] stopped: the dashboard cancelled this command\n"
            status = "succeeded" if code == 0 and not (timed_out or cancelled) else "failed"
        # Large final output goes up in chunks so nothing is cut at the 64 KiB limit.
        while len(tail) > MAX_CHUNK:
            self._post(cid, "running", tail[:MAX_CHUNK], retries=2)
            tail = tail[MAX_CHUNK:]
        self._post(cid, status, tail, code, retries=5)
        log.info("Command %s finished: %s (exit %s)", cid, status, code)

    def _follow_file(self, cid: str, plan: Plan) -> None:
        """Stream new lines of a log file (surviving rotation and truncation) until the
        time is up or the dashboard stops it. One message per FLUSH_EVERY at most, so a busy
        log costs no more relay commands than a quiet one."""
        path, wanted = plan.follow_file, plan.line_filter.encode()
        out: list[bytes] = []
        skipped = 0

        def keep(line: bytes, notice: bool = False) -> None:
            nonlocal skipped
            if wanted and wanted not in line and not notice:
                return
            out.append(line if line.endswith(b"\n") else line + b"\n")
            if len(out) > 2000:  # flooding: keep the newest, say how many were dropped
                skipped += len(out) - 2000
                del out[:-2000]

        def flush(status: str, extra: str = "") -> None:
            nonlocal skipped
            text = b"".join(out).decode("utf-8", errors="replace")
            if skipped:
                text = f"[agent] {skipped} lines skipped (too fast to stream)\n" + text
            out.clear()
            skipped = 0
            text += extra
            if text or status != "running":
                self._post(cid, status, text[-MAX_CHUNK:], 0 if status == "succeeded" else None,
                           retries=5 if status != "running" else 0)  # fmt: skip

        try:
            fh = open(path, "rb")  # noqa: SIM115 (reopened on rotation)
        except OSError as exc:
            self._post(cid, "failed", f"cannot read {path}: {exc.strerror}\n", None, retries=3)
            return
        try:
            for line in tail_file(path, 20).splitlines(keepends=True):
                keep(line.encode())
            fh.seek(0, os.SEEK_END)
            inode, pending = os.fstat(fh.fileno()).st_ino, b""
            deadline = time.monotonic() + plan.timeout
            last_flush, end = time.monotonic(), ""
            while not end:
                if self._is_cancelled(cid):
                    end = _follow_end(True, plan.timeout)
                elif time.monotonic() > deadline:
                    end = _follow_end(False, plan.timeout)
                chunk = fh.read(65536)
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        keep(line)
                    if len(pending) > 65536:  # a giant line without a newline
                        keep(pending)
                        pending = b""
                elif not end:
                    try:
                        st = os.stat(path)
                    except OSError:
                        st = None
                    if st is not None and (st.st_ino != inode or st.st_size < fh.tell()):
                        fh.close()
                        fh = open(path, "rb")  # noqa: SIM115
                        inode = os.fstat(fh.fileno()).st_ino
                        keep(b"[agent] the log was rotated; following the new file", notice=True)
                    time.sleep(0.5)
                if not end and time.monotonic() - last_flush >= FLUSH_EVERY:
                    last_flush = time.monotonic()
                    flush("running")
            if pending:
                keep(pending)
            flush("succeeded", f"\n[agent] {end}\n")
        finally:
            fh.close()

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


# --------------------------------------------------------------------------- probes

# Never probed, even when assigned: cloud metadata services hand out credentials, and
# link-local / unspecified / multicast addresses have no business in a health check.
# Private and loopback addresses ARE allowed: checking internal URLs is the point.
PROBE_BLOCKED = [
    ipaddress.ip_network(n)
    for n in ("0.0.0.0/8", "169.254.0.0/16", "100.100.100.200/32", "224.0.0.0/4",
              "255.255.255.255/32", "::/128", "fe80::/10", "fd00:ec2::254/128", "ff00::/8")
]  # fmt: skip
MAX_PROBES = 20
PROBE_BODY_LIMIT = 1024 * 1024
STATUS_SPEC_RE = re.compile(r"^\s*\d{3}(\s*-\s*\d{3})?(\s*,\s*\d{3}(\s*-\s*\d{3})?)*\s*$")


class ProbeBlocked(Exception):
    pass


def probe_address(host: str, port: int) -> str:
    """Resolve once and check: the address checked is the address connected to."""
    infos = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    ip = infos[0][4][0]
    addr = ipaddress.ip_address(ip.split("%")[0])
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    if any(addr in net for net in PROBE_BLOCKED):
        raise ProbeBlocked(f"{host} resolves to {addr}, which probes never contact")
    return ip


def clean_probe(spec) -> dict | None:
    """One assignment from the dashboard, validated (signed, but still bounded here)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("id"), str):
        return None
    if not COMMAND_ID_RE.match(spec["id"]):
        return None
    try:
        out = {"id": spec["id"], "kind": spec.get("kind"),
               "interval": max(30, min(3600, int(spec.get("interval") or 60))),
               "timeout": max(1, min(60, int(spec.get("timeout") or 10))),
               "verify_tls": spec.get("verify_tls") is not False,
               "slow_ms": int(spec["slow_ms"]) if spec.get("slow_ms") else None}  # fmt: skip
        if out["kind"] == "http":
            url = str(spec.get("url") or "")
            parts = urllib.parse.urlsplit(url)
            expected = str(spec.get("expected") or "200-399")
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return None
            if not STATUS_SPEC_RE.match(expected) or spec.get("method") not in (
                "GET",
                "HEAD",
                "POST",
                "PUT",
            ):
                return None
            out.update(url=url, method=spec["method"], expected=expected,
                       keyword=str(spec.get("keyword") or "")[:200],
                       keyword_absent=bool(spec.get("keyword_absent")),
                       follow_redirects=spec.get("follow_redirects") is not False,
                       body=str(spec.get("body") or "")[:4000])  # fmt: skip
        elif out["kind"] == "tcp":
            host, port = str(spec.get("host") or ""), int(spec.get("port") or 0)
            if not host or not 1 <= port <= 65535:
                return None
            out.update(host=host, port=port, tls=bool(spec.get("tls")))
        else:
            return None
    except (TypeError, ValueError):
        return None
    return out


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTP(S) to an already-checked IP, with the real host name for SNI and Host."""

    def __init__(self, host, port, ip, timeout, context, tls):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip, self._tls = ip, tls

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = (
            self._context.wrap_socket(sock, server_hostname=self.host) if self._tls else sock
        )


def _ranges(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def _probe_error(exc: BaseException) -> str:
    if isinstance(exc, ProbeBlocked):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS certificate invalid: {exc.verify_message}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    return (str(exc) or type(exc).__name__)[:200]


def run_http_probe(spec: dict) -> dict:
    ctx = ssl.create_default_context()
    if not spec["verify_tls"]:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    url, started = spec["url"], time.perf_counter()
    body = spec["body"].encode() if spec["method"] in ("POST", "PUT") and spec["body"] else None
    try:
        for _ in range(6):  # the request plus at most five redirects, each re-checked
            parts = urllib.parse.urlsplit(url)
            tls = parts.scheme == "https"
            port = parts.port or (443 if tls else 80)
            ip = probe_address(parts.hostname, port)
            conn = _PinnedConnection(parts.hostname, port, ip, spec["timeout"], ctx, tls)
            try:
                path = parts.path or "/"
                conn.request(spec["method"], path + (f"?{parts.query}" if parts.query else ""),
                             body=body, headers={"User-Agent": f"ServerMonitorAgent/{VERSION} (probe)"})  # fmt: skip
                resp = conn.getresponse()
                code = resp.status
                location = resp.getheader("Location")
                want = bool(spec["keyword"]) and spec["method"] != "HEAD"
                content = resp.read(PROBE_BODY_LIMIT) if want else b""
            finally:
                conn.close()
            if spec["follow_redirects"] and code in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                    return {"ok": False, "error": "redirect to a non-HTTP URL"}
                continue
            break
        else:
            return {"ok": False, "error": "too many redirects"}
    except (OSError, ProbeBlocked, http.client.HTTPException, ValueError) as exc:
        return {"ok": False, "error": _probe_error(exc)}
    ms = int((time.perf_counter() - started) * 1000)
    if not any(lo <= code <= hi for lo, hi in _ranges(spec["expected"])):
        return {"ok": False, "code": code, "ms": ms, "error": f"HTTP {code}"}
    if spec["keyword"] and spec["method"] != "HEAD":
        found = spec["keyword"].encode() in content
        if found == spec["keyword_absent"]:
            verb = "appeared" if found else "not found"
            return {
                "ok": False,
                "code": code,
                "ms": ms,
                "error": f"keyword {spec['keyword']!r} {verb}",
            }
    return {"ok": True, "code": code, "ms": ms}


def run_tcp_probe(spec: dict) -> dict:
    started = time.perf_counter()
    try:
        ip = probe_address(spec["host"], spec["port"])
        with socket.create_connection((ip, spec["port"]), timeout=spec["timeout"]) as sock:
            if spec["tls"]:
                ctx = ssl.create_default_context()
                if not spec["verify_tls"]:
                    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                with ctx.wrap_socket(sock, server_hostname=spec["host"]):
                    pass
    except (OSError, ProbeBlocked, ValueError) as exc:
        return {"ok": False, "error": _probe_error(exc)}
    return {"ok": True, "ms": int((time.perf_counter() - started) * 1000)}


def run_probe(spec: dict) -> dict:
    try:
        result = (run_http_probe if spec["kind"] == "http" else run_tcp_probe)(spec)
    except Exception as exc:  # a probe bug must never stop the others
        log.exception("Probe %s crashed", spec.get("id"))
        result = {"ok": False, "error": f"agent error: {exc}"[:200]}
    if result["ok"] and spec.get("slow_ms") and (result.get("ms") or 0) > spec["slow_ms"]:
        result.update(degraded=True, error=f"slow: {result['ms']} ms (limit {spec['slow_ms']} ms)")
    result["id"] = spec["id"]
    return result


class Prober:
    """Runs the checks the dashboard assigned, each at most once per heartbeat interval,
    and keeps the latest results for the next heartbeat (no extra Redis commands)."""

    def __init__(self, interval_fn):
        self.interval_fn = interval_fn
        self.lock = threading.Lock()
        self.specs: list[dict] = []
        self.rev = ""
        self.due: dict[str, float] = {}
        self.results: dict[str, list[dict]] = {}

    def assign(self, specs, rev) -> None:
        cleaned = [c for c in (clean_probe(s) for s in (specs or [])[:MAX_PROBES]) if c]
        with self.lock:
            self.specs, self.rev = cleaned, str(rev or "")[:64]
            ids = {s["id"] for s in cleaned}
            self.due = {k: v for k, v in self.due.items() if k in ids}
            self.results = {k: v for k, v in self.results.items() if k in ids}
        log.info("Probing %d target(s) for the dashboard", len(cleaned))

    def take(self) -> list[dict]:
        with self.lock:
            out = [r for rs in self.results.values() for r in rs]
            self.results = {}
        return out

    def put_back(self, results: list[dict]) -> None:
        """A heartbeat failed: keep its results for the next one (newest 3 per probe)."""
        with self.lock:
            for r in results:
                kept = self.results.setdefault(r["id"], [])
                kept.insert(0, r)
                del kept[:-3]

    def run_due(self) -> int:
        now = time.monotonic()
        with self.lock:
            due = [s for s in self.specs if self.due.get(s["id"], 0) <= now]
            for s in due:
                self.due[s["id"]] = now + max(s["interval"], self.interval_fn())
        for spec in due:
            result = run_probe(spec)
            with self.lock:
                if any(s["id"] == spec["id"] for s in self.specs):
                    kept = self.results.setdefault(spec["id"], [])
                    kept.append(result)
                    del kept[:-3]
        return len(due)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_due()
            stop.wait(5)


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
        self.prober = Prober(lambda: self.report_interval) if cfg.probes else None

    def request_stop(self) -> None:
        self.stop.set()
        self.relay.reader.interrupt()  # don't sit out a 2-minute BLPOP on shutdown

    def _backoff(self) -> float:
        self.failures += 1
        return min(300.0, 2 ** min(self.failures, 8)) * (0.5 + random.random() / 2)

    def heartbeat(self) -> bool:
        try:
            report = self.collector.report()
            results = []
            if self.prober is not None:
                results = self.prober.take()
                report["probe_results"], report["probes_rev"] = results, self.prober.rev
            try:
                self.relay.send({"t": "hb", "interval": self.report_interval, "report": report})
            except BaseException:
                if self.prober is not None:
                    self.prober.put_back(results)
                raise
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
            interval = max(10, int(message.get("report_interval") or self.report_interval))
            if interval != self.report_interval:
                self.report_interval = interval
                log.info("Report interval set to %ss by the dashboard", interval)
            if "probes" in message:
                if self.prober is None:
                    log.warning("The dashboard assigned probes, but [probes] enabled = false here")
                else:
                    self.prober.assign(message.get("probes"), message.get("probes_rev"))

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
        if self.prober is not None:
            threading.Thread(
                target=self.prober.run_forever, args=(self.stop,), name="probes", daemon=True
            ).start()
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


CGROUP_GLOBS = (
    "/sys/fs/cgroup/system.slice/docker-*.scope",
    "/sys/fs/cgroup/docker/*",
    "/sys/fs/cgroup/machine.slice/libpod-*.scope",
)
CONTAINER_ID_RE = re.compile(r"(?:docker-|libpod-|/docker/)([0-9a-f]{64})")
# What this agent can do, so the dashboard only offers features it supports.
FEATURES = ["job_ping", "inventory", "log_follow"]

JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def send_job_ping(cfg: Config, name: str, status: str, exit_code: int | None) -> int:
    """One signed heartbeat for a cron/backup job; the dashboard matches it by name."""
    if not JOB_NAME_RE.match(name):
        print(
            "job names use lowercase letters, digits, dots, dashes or underscores", file=sys.stderr
        )
        return 2
    message = {"t": "ping", "name": name, "status": status}
    if exit_code is not None:
        message["exit"] = exit_code
    relay = Relay(cfg)
    try:
        relay.send(message)
    except (RedisError, OSError, ConnectionError) as exc:
        print(f"could not reach the relay: {exc}", file=sys.stderr)
        return 4
    finally:
        relay.close()
    return 0


def send_command_result(cfg: Config, cid: str, status: str, exit_code: int | None) -> int:
    """Report a detached command's result (stdin is its output): `smctl upgrade
    --from-request` runs as root, then reports through the agent's own signed channel."""
    if not COMMAND_ID_RE.match(cid):
        print("bad command id", file=sys.stderr)
        return 2
    output = sys.stdin.read()[-MAX_CHUNK:] if not sys.stdin.isatty() else ""
    relay = Relay(cfg)
    try:
        relay.send({"t": "res", "id": cid, "status": status, "output": output,
                    "exit_code": exit_code})  # fmt: skip
    except (RedisError, OSError, ConnectionError) as exc:
        print(f"could not reach the relay: {exc}", file=sys.stderr)
        return 4
    finally:
        relay.close()
    return 0


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
    parser.add_argument(
        "--job-ping",
        metavar="NAME",
        help="Report a heartbeat for cron job NAME and exit (smctl ping)",
    )
    parser.add_argument("--job-status", choices=("ok", "fail", "start"), default="ok")
    parser.add_argument("--job-exit-code", type=int, default=None)
    parser.add_argument("--command-result", metavar="ID", help=argparse.SUPPRESS)
    parser.add_argument("--result-status", choices=("succeeded", "failed"), default="failed")
    parser.add_argument("--result-exit-code", type=int, default=None)
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
    if opts.job_ping:
        return send_job_ping(cfg, opts.job_ping, opts.job_status, opts.job_exit_code)
    if opts.command_result:
        return send_command_result(
            cfg, opts.command_result, opts.result_status, opts.result_exit_code
        )
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
