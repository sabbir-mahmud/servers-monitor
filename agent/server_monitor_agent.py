#!/usr/bin/env python3
"""
Server Monitor agent ("child").

Runs on every monitored server. It only ever dials OUT to main over HTTPS:

  every report_interval s   POST /api/agent/v1/heartbeat/  metrics, services, capabilities
                            ← schedule + any queued commands
  every command_poll s      GET  /api/agent/v1/commands/   ← queued commands
  while a command runs      POST /api/agent/v1/commands/<id>/  streamed output, then result

Security model: the agent's local config is the allowlist. Main can ask for an action by
name; the agent runs it only if this file declares it. Free-form shell is off unless
`[shell] enabled = true`. Commands are executed as argv lists (no shell) except that one
opt-in action.

Dependencies: Python 3.11+ (tomllib) and psutil.
"""

from __future__ import annotations

import argparse
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
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil

VERSION = "1.0.0"
API_PREFIX = "/api/agent/v1"
HTTP_TIMEOUT = 15
MAX_CHUNK = 60 * 1024  # main accepts 64 KiB per progress post
FLUSH_EVERY = 2.0  # seconds between streamed output posts
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
    url: str
    token: str
    verify_tls: bool = True
    ca_file: str | None = None
    report_interval: int = 30
    command_poll_interval: int = 5
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
    url = os.environ.get("SM_URL") or main.get("url", "")
    token = os.environ.get("SM_TOKEN") or main.get("token", "")
    if not url or not token:
        raise ConfigError("[main] url and token are required")
    url = url.rstrip("/")
    if url.startswith("http://") and not main.get("allow_insecure_http", False):
        # The bearer token would cross the network in clear text.
        raise ConfigError(
            "main.url must be https:// (set allow_insecure_http = true for local dev)"
        )

    agent = raw.get("agent", {})
    cfg = Config(
        url=url,
        token=token,
        verify_tls=bool(main.get("verify_tls", True)),
        ca_file=main.get("ca_file"),
        report_interval=int(agent.get("report_interval", 30)),
        command_poll_interval=int(agent.get("command_poll_interval", 5)),
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
            except OSError, IndexError:
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
                    except ValueError, KeyError, TypeError:
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


class Api:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if cfg.verify_tls:
            self.ctx = ssl.create_default_context(cafile=cfg.ca_file)
        else:
            self.ctx = ssl._create_unverified_context()  # noqa: S323 - explicit opt-out
        self.headers = {
            "Authorization": f"Bearer {cfg.token}",
            "Content-Type": "application/json",
            "User-Agent": f"server-monitor-agent/{VERSION}",
        }

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.cfg.url + API_PREFIX + path, data=data, headers=self.headers, method=method
        )
        ctx = self.ctx if req.full_url.startswith("https://") else None
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read() or b"{}")


class Executor:
    def __init__(self, cfg: Config, api: Api):
        self.cfg, self.api = cfg, api
        self.pool = ThreadPoolExecutor(
            max_workers=cfg.max_concurrent_commands, thread_name_prefix="cmd"
        )
        self._seen: deque[str] = deque(maxlen=500)
        self._lock = threading.Lock()

    def submit(self, command: dict) -> None:
        cid = command.get("id")
        with self._lock:
            # Main delivers each command once; this guards against a replayed response.
            if not cid or cid in self._seen:
                return
            self._seen.append(cid)
        self.pool.submit(self._run_safely, command)

    def _post(
        self,
        cid: str,
        status: str,
        output: str = "",
        exit_code: int | None = None,
        retries: int = 0,
    ) -> dict:
        body = {"status": status, "output": output[-MAX_CHUNK:], "exit_code": exit_code}
        for attempt in range(retries + 1):
            try:
                return self.api.call("POST", f"/commands/{cid}/", body)
            except (OSError, ValueError) as exc:
                if attempt == retries:
                    log.warning("Could not report command %s: %s", cid, exc)
                    return {}
                time.sleep(min(30, 2**attempt) + random.random())
        return {}

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
        timed_out = False
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
            time.sleep(0.2)
            chunk = ""
            if time.monotonic() - last_flush >= FLUSH_EVERY:
                last_flush = time.monotonic()
                chunk = drain()
            if chunk:
                reply = self._post(cid, "running", chunk)
                if reply.get("cancel"):
                    self._kill(proc)
                    break
        proc.wait()
        t.join(timeout=5)
        tail = drain()
        if timed_out:
            tail += f"\n[agent] killed after {plan.timeout}s timeout\n"
        code = proc.returncode
        status = "succeeded" if code == 0 and not timed_out else "failed"
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
        except ProcessLookupError, subprocess.TimeoutExpired:
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
        self.api = Api(cfg)
        self.collector = Collector(cfg)
        self.executor = Executor(cfg, self.api)
        self.stop = threading.Event()
        self.report_interval = cfg.report_interval
        self.poll_interval = cfg.command_poll_interval
        self.failures = 0

    def _apply(self, reply: dict) -> None:
        # Main owns the schedule: changing intervals in the dashboard retunes the agent.
        self.report_interval = max(5, int(reply.get("report_interval") or self.report_interval))
        self.poll_interval = max(2, int(reply.get("command_poll_interval") or self.poll_interval))
        for command in reply.get("commands") or []:
            self.executor.submit(command)

    def _backoff(self) -> float:
        self.failures += 1
        return min(300.0, 2 ** min(self.failures, 8)) * (0.5 + random.random() / 2)

    def heartbeat(self) -> bool:
        try:
            self._apply(self.api.call("POST", "/heartbeat/", self.collector.report()))
            self.failures = 0
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                log.error("Main rejected the token (401). Was it rotated? Check agent.toml.")
            else:
                log.warning("Heartbeat failed: HTTP %s", exc.code)
        except (OSError, ValueError) as exc:
            log.warning("Heartbeat failed: %s", exc)
        return False

    def poll(self) -> None:
        try:
            self._apply(self.api.call("GET", "/commands/"))
        except (OSError, ValueError) as exc:
            log.debug("Command poll failed: %s", exc)

    def run(self) -> None:
        log.info("Agent %s reporting to %s", VERSION, self.cfg.url)
        next_report = next_poll = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_report:
                ok = self.heartbeat()
                next_report = now + (self.report_interval if ok else self._backoff())
                next_poll = now + self.poll_interval
            elif now >= next_poll:
                self.poll()
                next_poll = now + self.poll_interval
            self.stop.wait(max(0.1, min(next_report, next_poll) - time.monotonic()))
        log.info("Stopping; waiting for running commands")
        self.executor.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Server Monitor agent")
    parser.add_argument("-c", "--config", default="/etc/server-monitor-agent/agent.toml")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    parser.add_argument(
        "--print", action="store_true", dest="print_only", help="Print the report JSON and exit"
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
    if opts.print_only:
        collector = Collector(cfg)
        time.sleep(0.5)  # let cpu_percent measure something
        print(json.dumps(collector.report(), indent=2))
        return 0
    agent = Agent(cfg)
    if opts.once:
        ok = agent.heartbeat()
        agent.executor.shutdown()
        return 0 if ok else 1
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: agent.stop.set())
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
