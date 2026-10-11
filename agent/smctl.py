#!/usr/bin/env python3
"""
smctl: manage the Server Monitor agent on this server.

The agent dials OUT to a Redis relay (the dashboard does too); nothing ever connects in,
so no SSH, no open ports and no public dashboard are needed.
This tool edits the agent's allowlist (/etc/server-monitor-agent/agent.toml), keeps the
sudo rules it needs in step, validates every change with the agent itself, and restarts
it. The dashboard shows the result on the next heartbeat.

  sudo smctl status                         is it running and connected?
  sudo smctl doctor                         check everything, explain what's wrong
  sudo smctl detect [--apply]               find services and logs on this host
  sudo smctl list                           show what the dashboard may see and run
  sudo smctl service add api --unit gunicorn-api --restartable
  sudo smctl log add api /var/www/api/logs/app.log
  sudo smctl action add deploy --sudo -- /opt/deploy/deploy.sh
  sudo smctl connect 'rediss://sm-...'      install rotated credentials
  sudo smctl upgrade | uninstall

Standard library only (Python 3.11+), so it runs before the agent's venv exists.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONF = Path(os.environ.get("SMCTL_CONF", "/etc/server-monitor-agent/agent.toml"))
PREFIX = Path(os.environ.get("SMCTL_PREFIX", "/opt/server-monitor-agent"))
SUDOERS = Path(os.environ.get("SMCTL_SUDOERS", "/etc/sudoers.d/server-monitor"))
AGENT_PY = PREFIX / "server_monitor_agent.py"
AGENT_PYTHON = PREFIX / "venv" / "bin" / "python"
UNIT = "server-monitor-agent"
AGENT_USER = "server-monitor"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# What a systemd unit / container / pm2 app may be called here. Nothing that sudoers could
# read as syntax (`,` separates commands, spaces add arguments, `!` negates, `*` globs).
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
SERVICE_KINDS = ("systemd", "docker", "pm2", "process", "http")
RESERVED = {"tail_log", "tail_follow", "restart_service", "shell", "_upgrade"}
STATE_DIR = Path(os.environ.get("SMCTL_STATE", "/var/lib/server-monitor-agent"))
UPGRADE_REQUEST = STATE_DIR / "upgrade-request.json"
UPGRADE_UNITS = ("server-monitor-upgrade.path", "server-monitor-upgrade.service")
# What a dashboard-requested upgrade may contain (checked again here, as root).
REF_RE = re.compile(r"^v\d{1,4}\.\d{1,4}\.\d{1,4}$")
SUM_RE = re.compile(r"^[0-9a-f]{64}$")
COMMAND_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
UPGRADE_FILES = ("install", "agent", "smctl", "unit", "upath", "uservice")

HEADER = """\
# Server Monitor agent: the allowlist for this server. Managed by `smctl`.
# The dashboard can only offer (and the agent will only run) what is declared here.
# Edit with `sudo smctl ...` or `sudo smctl edit`; both validate and restart the agent.
"""


# --------------------------------------------------------------------------- output

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(msg: str) -> None:
    print(f"  {_c('32', '✓')} {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', '!')} {msg}")


def bad(msg: str) -> None:
    print(f"  {_c('31', '✗')} {msg}")


def head(msg: str) -> None:
    print(_c("1", msg))


def dim(text: str) -> str:
    return _c("2", text)


class Fail(Exception):
    """A user-facing error: printed without a traceback, exit code 1."""


# --------------------------------------------------------------------------- helpers


def run(argv: list[str], timeout: float = 15, user: str | None = None) -> tuple[int, str]:
    if user and os.geteuid() == 0:
        if shutil.which("runuser"):
            argv = ["runuser", "-u", user, "--", *argv]
        else:
            argv = ["sudo", "-n", "-u", user, *argv]
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def need_root() -> None:
    if os.geteuid() != 0:
        raise Fail("this needs root: run it with sudo")


def check_name(name: str, what: str) -> str:
    if not NAME_RE.match(name):
        raise Fail(f"{what} name {name!r}: use letters, digits, '_', '-', '.' (max 64)")
    return name


# --------------------------------------------------------------------------- toml


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def dump_toml(data: dict) -> str:
    """Write the agent config schema back out: scalar tables, plus [services.*] and
    [actions.*] sub-tables. Comments are not preserved; HEADER explains that."""
    out = [HEADER]

    def table(name: str, body: dict) -> None:
        out.append(f"\n[{name}]")
        for k, v in body.items():
            if not isinstance(v, dict) or k == "env":
                out.append(f"{_key(k)} = {_toml_value(v)}")

    for section in ("main", "agent"):
        if section in data:
            table(section, data[section])
    for section in ("services", "actions"):
        for name, body in data.get(section, {}).items():
            table(f"{section}.{_key(name)}", body)
    table("logs", data.get("logs", {}))
    table("shell", data.get("shell", {"enabled": False}))
    for section, body in data.items():  # anything else a human added, kept verbatim
        if section not in {"main", "agent", "services", "actions", "logs", "shell"}:
            table(section, body)
    return "\n".join(out) + "\n"


def load() -> dict:
    try:
        with CONF.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise Fail(f"{CONF} not found: install the agent first") from None
    except PermissionError:
        raise Fail(f"cannot read {CONF}: run with sudo") from None
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{CONF} is not valid TOML: {exc} (fix it with `sudo smctl edit`)") from None


def validate_text(text: str) -> None:
    """Ask the installed agent to parse the candidate config, so smctl can never write a
    file the agent would refuse to start with."""
    if not AGENT_PY.exists():
        tomllib.loads(text)
        return
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(text)
        tmp = fh.name
    try:
        python = str(AGENT_PYTHON) if AGENT_PYTHON.exists() else sys.executable
        code, out = run([python, str(AGENT_PY), "-c", tmp, "--check"])
    finally:
        os.unlink(tmp)
    if code != 0:
        raise Fail("the agent rejected this change:\n    " + out.splitlines()[-1])


def save(data: dict, restart: bool = True) -> None:
    need_root()
    text = dump_toml(data)
    validate_text(text)
    tmp = CONF.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, 0, grp.getgrnam(AGENT_USER).gr_gid)
    except KeyError:
        pass
    os.replace(tmp, CONF)
    sync_sudoers(data)
    if restart:
        restart_agent()


def restart_agent() -> None:
    if not shutil.which("systemctl"):
        return
    code, out = run(["systemctl", "restart", UNIT])
    if code == 0:
        ok("agent restarted; the dashboard updates on its next heartbeat")
    else:
        warn(f"could not restart {UNIT}: {out}")


# --------------------------------------------------------------------------- sudoers

_SUDO_UNSAFE = re.compile(r"[\s,:=\\*?\[\]!#\"']")


def sudo_rules(data: dict) -> list[str]:
    """Exact-argv NOPASSWD rules for restartable systemd units and --sudo actions. Each
    rule names one full command line, so the agent user gains nothing beyond it."""
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    cmds = []
    for name, svc in data.get("services", {}).items():
        if svc.get("restartable") and svc.get("kind", "systemd") == "systemd":
            if not svc.get("restart_command"):
                unit = str(svc.get("unit") or name)
                # Checked again here, not just at `service add`: a hand-edited agent.toml
                # with "nginx, /bin/sh" would otherwise become a root shell rule.
                if not UNIT_RE.match(unit):
                    raise Fail(f"service {name}: unit {unit!r} has characters that aren't allowed")
                cmds.append(f"{systemctl} restart {unit}")
    for act in data.get("actions", {}).values():
        argv = act.get("command") or []
        if len(argv) > 2 and argv[:2] == ["sudo", "-n"]:
            cmds.append(" ".join(argv[2:]))
    return sorted(set(cmds))


def sync_sudoers(data: dict) -> None:
    rules = sudo_rules(data)
    if not rules:
        if SUDOERS.exists():
            SUDOERS.unlink()
        return
    body = (
        "# Managed by smctl: exact commands the Server Monitor agent may run as root.\n"
        + "".join(f"{AGENT_USER} ALL=(root) NOPASSWD: {r}\n" for r in rules)
    )
    if SUDOERS.exists() and SUDOERS.read_text() == body:
        return
    tmp = SUDOERS.with_name(".server-monitor.tmp")
    tmp.write_text(body)
    os.chmod(tmp, 0o440)
    if shutil.which("visudo"):
        code, out = run(["visudo", "-cf", str(tmp)])
        if code != 0:
            tmp.unlink()
            raise Fail(f"generated sudoers did not validate, nothing changed: {out}")
    os.replace(tmp, SUDOERS)
    ok(f"sudo rules updated ({len(rules)} command{'s' if len(rules) != 1 else ''})")


# --------------------------------------------------------------------------- detection

KNOWN_UNITS = re.compile(
    r"^(nginx|apache2|httpd|caddy|haproxy|traefik|postgresql(@[\w.-]+)?|mysql|mysqld|mariadb|"
    r"mongod|redis(-server)?|valkey(-server)?|memcached|rabbitmq-server|php[\d.]*-fpm|"
    r"gunicorn[\w@.-]*|uvicorn[\w@.-]*|daphne[\w@.-]*|celery[\w@.-]*|supervisor|"
    r"elasticsearch|opensearch|minio|docker|pm2-[\w.-]+)\.service$"
)
IGNORE_UNITS = re.compile(
    r"^(server-monitor-agent|ssh|sshd|cron|systemd-.*|getty@.*|user@.*)\.service$"
)
VERSION_COMMANDS = {
    "nginx": ["nginx", "-v"],
    "apache2": ["apache2", "-v"],
    "httpd": ["httpd", "-v"],
    "caddy": ["caddy", "version"],
    "haproxy": ["haproxy", "-v"],
    "postgresql": ["psql", "--version"],
    "mysql": ["mysql", "--version"],
    "mariadb": ["mariadb", "--version"],
    "redis-server": ["redis-server", "--version"],
    "redis": ["redis-server", "--version"],
    "mongod": ["mongod", "--version"],
    "docker": ["docker", "--version"],
    "memcached": ["memcached", "-V"],
}
LOG_CANDIDATES = [
    ("syslog", "/var/log/syslog"),
    ("messages", "/var/log/messages"),
    ("nginx_error", "/var/log/nginx/error.log"),
    ("nginx_access", "/var/log/nginx/access.log"),
    ("apache_error", "/var/log/apache2/error.log"),
    ("apache_access", "/var/log/apache2/access.log"),
    ("httpd_error", "/var/log/httpd/error_log"),
    ("mysql_error", "/var/log/mysql/error.log"),
    ("redis", "/var/log/redis/redis-server.log"),
    ("mongod", "/var/log/mongodb/mongod.log"),
]
SAFE_ACTIONS = {
    "disk_usage": {
        "description": "Filesystem usage (df -h)",
        "command": ["df", "-h"],
        "timeout": 30,
    },
    "top_processes": {
        "description": "Processes by CPU",
        "command": ["ps", "-eo", "pid,user,pcpu,pmem,etime,comm", "--sort=-pcpu"],
        "timeout": 30,
    },
}


@dataclass
class Found:
    section: str  # services | logs | actions
    name: str
    body: object
    note: str = ""


def _service_name(unit: str) -> str:
    base = unit.removesuffix(".service").replace("@", "-")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", base)[:64]


def detect_systemd(runner=run) -> list[Found]:
    if not shutil.which("systemctl") and runner is run:
        return []
    code, out = runner(
        ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"]
    )
    if code != 0:
        return []
    units = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    units = [u for u in units if u.endswith(".service") and not IGNORE_UNITS.match(u)]
    if not units:
        return []
    # Units an admin installed under /etc/systemd/system are almost always their own apps.
    code, out = runner(["systemctl", "show", "-p", "Id", "-p", "FragmentPath", *units])
    custom, current = set(), None
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "Id":
            current = value
        elif key == "FragmentPath" and value.startswith("/etc/systemd/system/") and current:
            custom.add(current)
    found = []
    for unit in units:
        if not (KNOWN_UNITS.match(unit) or unit in custom):
            continue
        if not UNIT_RE.match(unit.removesuffix(".service")):
            continue
        name = _service_name(unit)
        body: dict = {"kind": "systemd", "unit": unit.removesuffix(".service")}
        base = name.split("-")[0] if name.startswith("postgresql") else name
        if base in VERSION_COMMANDS:
            body["version_command"] = VERSION_COMMANDS[base]
        found.append(Found("services", name, body, "custom unit" if unit in custom else ""))
    return found


def _agent_in_group(group: str) -> bool:
    try:
        return AGENT_USER in grp.getgrnam(group).gr_mem
    except KeyError:
        return False


def detect_docker(runner=run) -> list[Found]:
    if not shutil.which("docker") and runner is run:
        return []
    code, out = runner(["docker", "ps", "--format", "{{.Names}}"])
    if code != 0:
        return []
    found = []
    for name in out.split():
        found.append(Found("services", _service_name(name), {"kind": "docker", "unit": name}))
    return found


def detect_logs(exists=os.path.isfile) -> list[Found]:
    found = [Found("logs", n, p) for n, p in LOG_CANDIDATES if exists(p)]
    pg = (
        sorted(Path("/var/log/postgresql").glob("postgresql-*-main.log"))
        if exists is os.path.isfile
        else []
    )
    if pg:
        found.append(Found("logs", "postgresql", str(pg[-1])))
    return found


def detect(runner=run) -> tuple[list[Found], list[str]]:
    notes = []
    found = detect_systemd(runner)
    docker = detect_docker(runner)
    if docker and not _agent_in_group("docker"):
        notes.append(
            f"{len(docker)} docker container(s) found. Monitoring them needs the agent in the "
            "`docker` group, which is root-equivalent. Opt in with: sudo smctl docker enable"
        )
    else:
        found += docker
    found += detect_logs()
    found += [Found("actions", n, dict(b), "safe, read-only") for n, b in SAFE_ACTIONS.items()]
    return found, notes


# --------------------------------------------------------------------------- commands


def cmd_detect(opts) -> None:
    found, notes = detect()
    data = load() if CONF.exists() else {}
    new = [f for f in found if f.name not in data.get(f.section, {})]
    head("Detected on this server")
    if not found:
        print(dim("  nothing recognised; add services by hand with `smctl service add`"))
    for f in found:
        mark = _c("2", "· have") if f not in new else _c("32", "+ new ")
        desc = (
            f.body
            if isinstance(f.body, str)
            else f.body.get("unit") or " ".join(f.body.get("command", []))
        )
        extra = f"  {dim(f.note)}" if f.note else ""
        print(f"  {mark}  {f.section[:-1]:<8} {f.name:<22} {dim(desc)}{extra}")
    for n in notes:
        warn(n)
    if not opts.apply:
        if new:
            print(dim("\n  Add them with: sudo smctl detect --apply"))
        return
    if not new:
        ok("nothing new to add")
        return
    for f in new:
        data.setdefault(f.section, {})[f.name] = f.body
    save(data, restart=not opts.no_restart)
    ok(f"added {len(new)} item(s)")
    print(
        dim(
            "  Make a service restartable from the dashboard: sudo smctl service restartable <name> on"
        )
    )


def relay_host(data: dict) -> str:
    return urllib.parse.urlsplit(data.get("main", {}).get("redis_url", "")).hostname or "?"


def cmd_list(opts) -> None:
    data = load()
    head(f"Reporting through relay {relay_host(data)}")
    head("\nServices")
    for name, s in data.get("services", {}).items():
        flags = " restartable" if s.get("restartable") else ""
        print(
            f"  {name:<22} {s.get('kind', 'systemd'):<8} {dim(s.get('unit') or s.get('url') or name)}{_c('36', flags)}"
        )
    if not data.get("services"):
        print(dim("  none: sudo smctl detect --apply, or sudo smctl service add <name>"))
    head("\nLogs")
    for name, path in data.get("logs", {}).items():
        print(f"  {name:<22} {dim(path)}")
    if not data.get("logs"):
        print(dim("  none: sudo smctl log add <name> <path>"))
    head("\nActions")
    for name, a in data.get("actions", {}).items():
        argv = a.get("command", [])
        sudo = _c("36", " (root)") if argv[:2] == ["sudo", "-n"] else ""
        print(f"  {name:<22} {dim(shlex.join(argv[2:] if sudo else argv))}{sudo}")
    if not data.get("actions"):
        print(dim("  none: sudo smctl action add deploy -- /opt/deploy/deploy.sh"))
    shell = data.get("shell", {}).get("enabled", False)
    print(_c("1", "\nFree-form shell: ") + ("on" if shell else "off"))


def cmd_service(opts) -> None:
    data = load()
    services = data.setdefault("services", {})
    if opts.op == "add":
        name = check_name(opts.name, "service")
        if name in services and not opts.force:
            raise Fail(f"service {name!r} exists (use --force to replace)")
        if opts.kind not in SERVICE_KINDS:
            raise Fail(f"--kind must be one of {', '.join(SERVICE_KINDS)}")
        body: dict = {"kind": opts.kind, "unit": opts.unit or name}
        if opts.kind in ("systemd", "docker", "pm2") and not UNIT_RE.match(body["unit"]):
            raise Fail(f"unit {body['unit']!r}: use letters, digits and _ . : @ - only")
        if opts.kind == "http":
            body = {"kind": "http", "url": opts.unit or ""}
            if not body["url"].startswith(("http://", "https://")):
                raise Fail("http services need --unit http(s)://host/health")
        if opts.restartable:
            if opts.kind not in ("systemd", "docker", "pm2"):
                raise Fail(f"{opts.kind} services can't be restarted by the agent")
            body["restartable"] = True
        if opts.version_command:
            body["version_command"] = shlex.split(opts.version_command)
        if opts.version_file:
            body["version_file"] = opts.version_file
        services[name] = body
        save(data)
        ok(f"service {name} added")
    elif opts.op == "rm":
        if services.pop(opts.name, None) is None:
            raise Fail(f"no service named {opts.name!r}")
        save(data)
        ok(f"service {opts.name} removed")
    elif opts.op == "restartable":
        svc = services.get(opts.name)
        if svc is None:
            raise Fail(f"no service named {opts.name!r}")
        on = opts.state == "on"
        if on and svc.get("kind", "systemd") not in ("systemd", "docker", "pm2"):
            raise Fail(f"{svc.get('kind')} services can't be restarted by the agent")
        svc["restartable"] = on
        save(data)
        ok(f"{opts.name}: restart button {'enabled' if on else 'disabled'}")


def cmd_log(opts) -> None:
    data = load()
    logs = data.setdefault("logs", {})
    if opts.op == "add":
        name = check_name(opts.name, "log")
        path = os.path.abspath(opts.path)
        if not os.path.isfile(path):
            warn(f"{path} does not exist yet; adding anyway")
        else:
            code, _ = run(["test", "-r", path], user=AGENT_USER)
            if code != 0:
                warn(f"the agent user can't read {path}; tailing will fail until it can")
        logs[name] = path
        save(data)
        ok(f"log {name} → {path}")
    else:
        if logs.pop(opts.name, None) is None:
            raise Fail(f"no log named {opts.name!r}")
        save(data)
        ok(f"log {opts.name} removed")


def cmd_action(opts) -> None:
    data = load()
    actions = data.setdefault("actions", {})
    if opts.op == "add":
        name = check_name(opts.name, "action")
        if name in RESERVED:
            raise Fail(f"{name!r} is a built-in action name")
        if name in actions and not opts.force:
            raise Fail(f"action {name!r} exists (use --force to replace)")
        argv = list(opts.command)
        if not argv:
            raise Fail("give the command after --, e.g. smctl action add deploy -- /opt/deploy.sh")
        if opts.sudo:
            if not argv[0].startswith("/"):
                resolved = shutil.which(argv[0])
                if not resolved:
                    raise Fail(f"{argv[0]}: not found; use an absolute path with --sudo")
                argv[0] = resolved
            if any(_SUDO_UNSAFE.search(a) for a in argv):
                raise Fail(
                    "--sudo commands can't contain spaces, quotes or , : = \\ * ? in arguments; wrap them in a script"
                )
            argv = ["sudo", "-n", *argv]
        elif not shutil.which(argv[0]) and not os.path.exists(argv[0]):
            warn(f"{argv[0]} not found on this server yet")
        body: dict = {"command": argv, "timeout": opts.timeout}
        if opts.description:
            body["description"] = opts.description
        if opts.cwd:
            body["cwd"] = opts.cwd
        actions[name] = body
        save(data)
        ok(f"action {name} added{' (runs as root via sudo)' if opts.sudo else ''}")
    elif opts.op == "rm":
        if actions.pop(opts.name, None) is None:
            raise Fail(f"no action named {opts.name!r}")
        save(data)
        ok(f"action {opts.name} removed")
    elif opts.op == "test":
        act = actions.get(opts.name)
        if act is None:
            raise Fail(f"no action named {opts.name!r}")
        print(dim(f"$ {shlex.join(act['command'])}   (as {AGENT_USER})"))
        need_root()
        p = subprocess.run(
            ["runuser", "-u", AGENT_USER, "--", *act["command"]]
            if shutil.which("runuser")
            else act["command"],
            cwd=act.get("cwd"),
            timeout=act.get("timeout", 300),
        )
        (ok if p.returncode == 0 else bad)(f"exit {p.returncode}")


def cmd_shell(opts) -> None:
    data = load()
    data.setdefault("shell", {})["enabled"] = opts.state == "on"
    if opts.state == "on":
        warn("free-form shell lets dashboard Admins run any command as the agent user")
    save(data)
    ok(f"free-form shell {opts.state}")


def cmd_probes(opts) -> None:
    """Let the dashboard assign HTTP/TCP checks to run from this server."""
    data = load()
    data.setdefault("probes", {})["enabled"] = opts.state == "on"
    if opts.state == "on":
        warn(
            "dashboard Admins can now have this server fetch URLs and open TCP connections, "
            "including internal ones (cloud metadata addresses are always refused)"
        )
    save(data)
    ok(f"uptime probes {opts.state}")


def cmd_docker(opts) -> None:
    need_root()
    if opts.state == "enable":
        warn("the docker group is root-equivalent: dashboard actions could control the host")
        run(["usermod", "-aG", "docker", AGENT_USER])
        ok(f"{AGENT_USER} added to the docker group; now run: sudo smctl detect --apply")
    else:
        run(["gpasswd", "-d", AGENT_USER, "docker"])
        ok(f"{AGENT_USER} removed from the docker group")
    # Container names (and stopped containers) on the dashboard need `docker ps`.
    data = load()
    data.setdefault("docker", {})["names"] = opts.state == "enable"
    save(data, restart=False)
    restart_agent()


def _read_connection_string(opts) -> str:
    """Hidden prompt (or stdin): a connection string on the command line would sit in
    `ps` output and shell history, readable by other users of this server."""
    if opts.redis_url and opts.redis_url != "-":
        warn(
            "the connection string was passed as an argument, so it's in your shell history; "
            "next time run `sudo smctl connect` and paste it when asked"
        )
        return opts.redis_url.strip()
    if opts.redis_url == "-" or not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    import getpass

    return getpass.getpass(
        "Paste the connection string from the dashboard (input hidden): "
    ).strip()


def cmd_connect(opts) -> None:
    url = _read_connection_string(opts)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("rediss", "redis") or not parts.username or not parts.password:
        raise Fail(
            "expected the connection string from the dashboard: rediss://sm-<id>:<password>@host:6379/?key=..."
        )
    if "key=" not in parts.query:
        warn(
            "this connection string has no signing key: commands won't be verified. "
            "Rotate the server's credentials in the dashboard to get a signed one."
        )
    data = load()
    data.setdefault("main", {})["redis_url"] = url
    data["main"].pop("server_id", None)  # derived from the username
    save(data)
    ok(f"connected as {parts.username}; check with: sudo smctl doctor")


def _heartbeat() -> tuple[bool, str]:
    if not AGENT_PY.exists():
        return False, "agent not installed"
    python = str(AGENT_PYTHON) if AGENT_PYTHON.exists() else sys.executable
    code, out = run([python, str(AGENT_PY), "-c", str(CONF), "--once"], timeout=30, user=AGENT_USER)
    last = out.splitlines()[-1] if out else ""
    return code == 0, last


def _job_ping(name: str, status: str, exit_code: int | None = None) -> tuple[bool, str]:
    if not AGENT_PY.exists():
        return False, "agent not installed"
    python = str(AGENT_PYTHON) if AGENT_PYTHON.exists() else sys.executable
    argv = [python, str(AGENT_PY), "-c", str(CONF), "--job-ping", name, "--job-status", status]
    if exit_code is not None:
        argv += ["--job-exit-code", str(exit_code)]
    code, out = run(argv, timeout=30, user=AGENT_USER)
    return code == 0, out


def cmd_ping(opts) -> None:
    """Tell the dashboard a cron job ran (a Heartbeat monitor with the same name)."""
    need_root()
    status = "fail" if opts.fail else "start" if opts.start else "ok"
    sent, out = _job_ping(opts.name, status, opts.exit_code)
    if not sent:
        raise Fail(f"ping not sent: {out}")
    if not opts.quiet:
        ok(f"{opts.name}: {status} sent")


def cmd_run(opts) -> None:
    """Run a command and report start, then success or failure with its exit code.
    Exits with the command's own exit code, so cron still sees failures."""
    need_root()
    if not opts.command:
        raise Fail("usage: smctl run NAME -- COMMAND [ARGS...]")
    _job_ping(opts.name, "start")
    try:
        code = subprocess.run(opts.command).returncode
    except FileNotFoundError:
        code = 127
        bad(f"{opts.command[0]}: not found")
    sent, out = _job_ping(opts.name, "ok" if code == 0 else "fail", None if code == 0 else code)
    if not sent:
        warn(f"the job finished (exit {code}) but the dashboard wasn't told: {out}")
    sys.exit(code)


def cmd_status(opts) -> None:
    data = load()
    code, state = run(["systemctl", "is-active", UNIT])
    _, since = run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", UNIT])
    head("Server Monitor agent")
    (ok if state == "active" else bad)(
        f"service {state}{dim('  since ' + since) if since and state == 'active' else ''}"
    )
    print(f"    relay      {relay_host(data)}")
    version = (
        next(
            (
                ln.split('"')[1]
                for ln in AGENT_PY.read_text().splitlines()
                if ln.startswith("VERSION")
            ),
            "?",
        )
        if AGENT_PY.exists()
        else "not installed"
    )
    print(f"    agent      {version}")
    print(f"    config     {CONF}")
    n = {k: len(data.get(k, {})) for k in ("services", "logs", "actions")}
    print(
        f"    allowlist  {n['services']} services · {n['logs']} logs · {n['actions']} actions"
        f" · shell {'on' if data.get('shell', {}).get('enabled') else 'off'}"
    )
    _, journal = run(["journalctl", "-u", UNIT, "-n", "30", "--no-pager", "-o", "cat"])
    errors = [ln for ln in journal.splitlines() if "ERROR" in ln or "WARNING" in ln]
    if errors:
        warn("recent problems in the agent log:")
        for ln in errors[-3:]:
            print(dim(f"      {ln[-140:]}"))
    if state != "active":
        print(dim("\n  Try: sudo smctl doctor"))


def cmd_doctor(opts) -> None:
    need_root()
    head("Checking the Server Monitor agent")
    problems = 0
    try:
        data = load()
        validate_text(CONF.read_text())
        ok("config is valid")
    except Fail as exc:
        bad(str(exc))
        return
    code, state = run(["systemctl", "is-active", UNIT])
    if state == "active":
        ok("agent service is running")
    else:
        problems += 1
        bad(f"agent service is {state}: sudo systemctl start {UNIT}; journalctl -u {UNIT}")
    python = str(AGENT_PYTHON) if AGENT_PYTHON.exists() else sys.executable
    code, out = run([python, str(AGENT_PY), "-c", str(CONF), "--ping"], timeout=30)
    if code == 0:
        ok(f"relay {relay_host(data)} reachable, credentials accepted")
    else:
        problems += 1
        hint = (
            " Rotate them in the dashboard, then: sudo smctl connect '<new string>'"
            if code == 3
            else " (DNS, outbound firewall on port 6379, or TLS?)"
        )
        bad((out.splitlines() or ["relay check failed"])[-1] + hint)
    sent, detail = _heartbeat()
    if sent:
        ok("heartbeat delivered to the dashboard's mailbox")
    else:
        problems += 1
        bad(f"heartbeat failed: {detail}")
    for name, path in data.get("logs", {}).items():
        if run(["test", "-r", path], user=AGENT_USER)[0] != 0:
            problems += 1
            bad(f"log {name}: {AGENT_USER} cannot read {path}")
    for name, svc in data.get("services", {}).items():
        if svc.get("kind", "systemd") == "systemd":
            c, _ = run(["systemctl", "cat", f"{svc.get('unit', name)}.service"])
            if c != 0:
                problems += 1
                bad(f"service {name}: systemd unit {svc.get('unit')} not found")
        if svc.get("restartable") and svc.get("kind") == "docker" and not _agent_in_group("docker"):
            problems += 1
            bad(f"service {name}: restart needs `sudo smctl docker enable`")
    rules = sudo_rules(data)
    if rules and (not SUDOERS.exists() or any(r not in SUDOERS.read_text() for r in rules)):
        problems += 1
        bad("sudo rules are out of date; fix with: sudo smctl sync")
    elif rules:
        ok(f"sudo rules in place for {len(rules)} command(s)")
    print()
    if problems:
        bad(f"{problems} problem(s) found")
        raise SystemExit(1)
    ok("everything looks good")


def cmd_sync(opts) -> None:
    need_root()
    data = load()
    sync_sudoers(data)
    restart_agent()


def cmd_edit(opts) -> None:
    need_root()
    editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or "vi"
    original = CONF.read_text()
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(original)
        tmp = fh.name
    try:
        while True:
            subprocess.run([*shlex.split(editor), tmp])
            text = Path(tmp).read_text()
            if text == original:
                print(dim("  no changes"))
                return
            try:
                validate_text(text)
                break
            except (Fail, tomllib.TOMLDecodeError) as exc:
                bad(str(exc))
                if input("  Edit again? [Y/n] ").strip().lower() == "n":
                    raise Fail("discarded; nothing changed") from None
        CONF.write_text(text)
        sync_sudoers(tomllib.loads(text))
        restart_agent()
    finally:
        os.unlink(tmp)


def cmd_logs(opts) -> None:
    os.execvp(
        "journalctl",
        [
            "journalctl",
            "-u",
            UNIT,
            "-n",
            str(opts.lines),
            "--no-pager",
            *(["-f"] if opts.follow else []),
        ],
    )


def cmd_restart(opts) -> None:
    need_root()
    restart_agent()


def _source() -> dict:
    """Where this agent was installed from (written by the installer, root-owned)."""
    source = {"SM_REPO": "sabbir-mahmud/servers-monitor", "SM_REF": "main"}
    try:
        for line in (PREFIX / "source").read_text().splitlines():
            key, _, value = line.partition("=")
            if key in source and value:
                source[key] = value
    except OSError:
        pass
    return source


def _upgrade(ref: str | None, sums: str | None, capture: bool = False) -> tuple[int, str]:
    """Run install.sh for `ref` from the recorded repository, verified against `sums`
    (None = unverified). Returns (exit code, output if captured)."""
    source = _source()
    if ref:
        source["SM_REF"] = ref
    url = (
        f"https://raw.githubusercontent.com/{source['SM_REPO']}/{source['SM_REF']}/agent/install.sh"
    )
    print(dim(f"  fetching installer from {url}"))
    try:
        script = urllib.request.urlopen(url, timeout=30).read()
    except OSError as exc:
        raise Fail(f"could not download the installer: {exc}") from None
    env = {**os.environ, **source}
    if sums:
        import hashlib

        parsed = dict(part.split("=", 1) for part in sums.split(",") if "=" in part)
        if hashlib.sha256(script).hexdigest() != parsed.get("install"):
            raise Fail(
                "install.sh from GitHub doesn't match the dashboard's checksum; refusing to run it"
            )
        env["SM_SUMS"] = sums
    else:
        env["SM_ALLOW_UNVERIFIED"] = "1"
    if not capture:
        return subprocess.run(["bash", "-s"], input=script, env=env).returncode, ""
    try:
        p = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=1100)
    except subprocess.TimeoutExpired:
        return 124, "the installer timed out\n"
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")


def read_upgrade_request(path: Path | None = None) -> dict:
    """Read and delete the request the agent wrote. The agent user owns that directory, so
    the file is untrusted: never follow a link (it could point root at /etc/shadow), refuse
    anything but a small single-linked regular file, never echo its content, and validate
    every field."""
    path = path or UPGRADE_REQUEST
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Fail("no upgrade request waiting") from None
    except OSError:
        path.unlink(missing_ok=True)
        raise Fail("the upgrade request is not a regular file; ignored") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 4096:
            raise Fail("the upgrade request is not a small regular file; ignored")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)  # handled (or refused) once; the path unit re-arms
    try:
        req = json.loads(raw)
    except ValueError:
        raise Fail("the upgrade request is not valid JSON; ignored") from None
    if not isinstance(req, dict):
        raise Fail("the upgrade request is not a JSON object; ignored")
    cid, ref, sums = req.get("id"), req.get("ref"), req.get("sums")
    if not isinstance(cid, str) or not COMMAND_ID_RE.match(cid):
        raise Fail("the upgrade request has no valid command id; ignored")
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise Fail("upgrades from the dashboard must name a release tag like v2.3.0")
    if not isinstance(sums, dict) or set(sums) != set(UPGRADE_FILES):
        raise Fail(f"the upgrade request needs checksums for {', '.join(UPGRADE_FILES)}")
    if not all(isinstance(v, str) and SUM_RE.match(v) for v in sums.values()):
        raise Fail("the upgrade request's checksums are not SHA-256 digests")
    return {"id": cid, "ref": ref, "sums": sums}


def report_result(cid: str, code: int, output: str) -> bool:
    """Send the upgrade's outcome as the dashboard job's result, signed by the agent."""
    python = str(AGENT_PYTHON) if AGENT_PYTHON.exists() else sys.executable
    argv = [python, str(AGENT_PY), "-c", str(CONF), "--command-result", cid,
            "--result-status", "succeeded" if code == 0 else "failed",
            "--result-exit-code", str(code)]  # fmt: skip
    if os.geteuid() == 0 and shutil.which("runuser"):
        argv = ["runuser", "-u", AGENT_USER, "--", *argv]
    try:
        p = subprocess.run(argv, input=output.encode(), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def upgrade_from_request() -> int:
    """The root side of a dashboard-requested upgrade (server-monitor-upgrade.service)."""
    need_root()
    req = read_upgrade_request()
    if not load().get("agent", {}).get("remote_upgrade", False):
        report_result(req["id"], 1, "rejected: remote upgrades are off on this server\n")
        raise Fail("remote upgrades are off (sudo smctl remote-upgrade on); request ignored")
    sums = ",".join(f"{k}={req['sums'][k]}" for k in UPGRADE_FILES)
    try:
        code, output = _upgrade(req["ref"], sums, capture=True)
    except Fail as exc:
        code, output = 1, f"{exc}\n"
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)  # no colour codes in the job output
    if not report_result(req["id"], code, output):
        warn("upgrade finished but the dashboard could not be told; it will show the job as lost")
    return code


def cmd_upgrade(opts) -> None:
    """Re-run the installer from GitHub, verified against checksums from the dashboard
    (server page → Install / upgrade). Without them, refuse unless --unverified: an
    unverified upgrade runs whatever is on GitHub right now, as root."""
    need_root()
    if opts.from_request:
        raise SystemExit(upgrade_from_request())
    if not opts.sums and not opts.unverified:
        raise Fail(
            "copy the upgrade command from the server's page in the dashboard (it includes "
            "--sums), or pass --unverified to trust GitHub as-is"
        )
    raise SystemExit(_upgrade(opts.ref, opts.sums)[0])


def cmd_remote_upgrade(opts) -> None:
    """Opt in to (or out of) upgrades requested from the dashboard."""
    need_root()
    data = load()
    data.setdefault("agent", {})["remote_upgrade"] = opts.state == "on"
    if opts.state == "on":
        warn(
            "dashboard Admins can now install any release tag of "
            f"{_source()['SM_REPO']} here (verified against the dashboard's checksums)"
        )
    save(data, restart=False)
    if shutil.which("systemctl"):
        verb = ["enable", "--now"] if opts.state == "on" else ["disable", "--now"]
        code, out = run(["systemctl", *verb, UPGRADE_UNITS[0]])
        if code != 0:
            warn(f"could not {verb[0]} {UPGRADE_UNITS[0]}: {out} (upgrade the agent once first)")
    restart_agent()
    ok(f"remote upgrades {opts.state}")


def cmd_uninstall(opts) -> None:
    need_root()
    if (
        not opts.yes
        and input("Remove the Server Monitor agent from this server? [y/N] ").strip().lower() != "y"
    ):
        raise Fail("aborted")
    run(["systemctl", "disable", "--now", UNIT])
    run(["systemctl", "disable", "--now", UPGRADE_UNITS[0]])
    for path in (
        Path(f"/etc/systemd/system/{UNIT}.service"),
        *(Path(f"/etc/systemd/system/{u}") for u in UPGRADE_UNITS),
        SUDOERS,
    ):
        path.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    shutil.rmtree(PREFIX, ignore_errors=True)
    if opts.purge:
        shutil.rmtree(CONF.parent, ignore_errors=True)
        run(["userdel", AGENT_USER])
        ok("agent, config and user removed")
    else:
        ok(f"agent removed; config kept in {CONF.parent} (use --purge to delete it)")
    Path("/usr/local/bin/smctl").unlink(missing_ok=True)
    print(dim("  Also delete the server in the dashboard so its token is revoked."))


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smctl",
        description="Manage the Server Monitor agent on this server.",
        epilog="Most commands need sudo. Every change is validated and applied immediately.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("status", help="is the agent running and connected?").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="check config, connectivity, token and permissions").set_defaults(
        fn=cmd_doctor
    )
    d = sub.add_parser("detect", help="find services and logs on this server")
    d.add_argument("--apply", action="store_true", help="add what was found")
    d.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    d.set_defaults(fn=cmd_detect)
    sub.add_parser("list", help="show services, logs and actions").set_defaults(fn=cmd_list)

    s = sub.add_parser("service", help="add or remove a monitored service")
    ssub = s.add_subparsers(dest="op", required=True)
    sa = ssub.add_parser("add", help="monitor a service")
    sa.add_argument("name")
    sa.add_argument(
        "--kind", default="systemd", help=f"one of {', '.join(SERVICE_KINDS)} (default systemd)"
    )
    sa.add_argument(
        "--unit", help="systemd unit, container, pm2 app, process match or URL (default: name)"
    )
    sa.add_argument(
        "--restartable", action="store_true", help="show a Restart button in the dashboard"
    )
    sa.add_argument("--version-command", help='e.g. "node --version"')
    sa.add_argument("--version-file", help="file whose first line is the version")
    sa.add_argument("--force", action="store_true")
    ssub.add_parser("rm", help="stop monitoring a service").add_argument("name")
    sr = ssub.add_parser("restartable", help="turn the Restart button on or off")
    sr.add_argument("name")
    sr.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_service)

    lg = sub.add_parser("log", help="add or remove a tail-able log file")
    lsub = lg.add_subparsers(dest="op", required=True)
    la = lsub.add_parser("add")
    la.add_argument("name")
    la.add_argument("path")
    lsub.add_parser("rm").add_argument("name")
    lg.set_defaults(fn=cmd_log)

    a = sub.add_parser("action", help="add, remove or test a one-click action (deploy, update…)")
    asub = a.add_subparsers(dest="op", required=True)
    aa = asub.add_parser("add", usage="smctl action add NAME [options] -- COMMAND [ARGS...]")
    aa.add_argument("name")
    aa.add_argument("-d", "--description", default="")
    aa.add_argument("-t", "--timeout", type=int, default=600, help="seconds (default 600)")
    aa.add_argument("--cwd", help="working directory")
    aa.add_argument("--sudo", action="store_true", help="run as root (adds an exact sudo rule)")
    aa.add_argument("--force", action="store_true")
    asub.add_parser("rm").add_argument("name")
    asub.add_parser("test", help="run it here, as the agent user").add_argument("name")
    a.set_defaults(fn=cmd_action)

    sh = sub.add_parser("shell", help="allow or forbid free-form shell from the dashboard")
    sh.add_argument("state", choices=["on", "off"])
    sh.set_defaults(fn=cmd_shell)
    pr = sub.add_parser("probes", help="run uptime checks for the dashboard from this server")
    pr.add_argument("state", choices=["on", "off"])
    pr.set_defaults(fn=cmd_probes)
    dk = sub.add_parser("docker", help="let the agent see docker containers (root-equivalent)")
    dk.add_argument("state", choices=["enable", "disable"])
    dk.set_defaults(fn=cmd_docker)
    t = sub.add_parser("connect", help="install new relay credentials (after rotating them)")
    t.add_argument(
        "redis_url",
        metavar="CONNECTION_STRING",
        nargs="?",
        help="omit to paste it at a hidden prompt (recommended); '-' reads stdin",
    )
    t.set_defaults(fn=cmd_connect)

    sub.add_parser("edit", help="edit agent.toml in $EDITOR, validated before saving").set_defaults(
        fn=cmd_edit
    )
    sub.add_parser("sync", help="rewrite sudo rules from the config and restart").set_defaults(
        fn=cmd_sync
    )
    sub.add_parser("restart", help="restart the agent").set_defaults(fn=cmd_restart)
    lo = sub.add_parser("logs", help="show the agent's own log")
    lo.add_argument("-f", "--follow", action="store_true")
    lo.add_argument("-n", "--lines", type=int, default=50)
    lo.set_defaults(fn=cmd_logs)
    pg = sub.add_parser("ping", help="report that a cron job ran (Heartbeat monitors)")
    pg.add_argument("name")
    pg_state = pg.add_mutually_exclusive_group()
    pg_state.add_argument("--fail", action="store_true", help="the job failed")
    pg_state.add_argument(
        "--start", action="store_true", help="the job is starting (measures runtime)"
    )
    pg.add_argument("--exit-code", type=int, help="the job's exit code (with --fail)")
    pg.add_argument("-q", "--quiet", action="store_true")
    pg.set_defaults(fn=cmd_ping)
    rn = sub.add_parser(
        "run",
        help="run a job and report it (Heartbeat monitors)",
        usage="smctl run NAME -- COMMAND [ARGS...]",
    )
    rn.add_argument("name")
    rn.set_defaults(fn=cmd_run)
    up = sub.add_parser("upgrade", help="install the latest agent from GitHub")
    up.add_argument("--ref", help="branch or tag to install (default: the one installed from)")
    up.add_argument("--sums", help="checksums from the dashboard's upgrade command")
    up.add_argument("--unverified", action="store_true", help="skip checksum verification")
    up.add_argument("--from-request", action="store_true", help=argparse.SUPPRESS)
    up.set_defaults(fn=cmd_upgrade)
    ru = sub.add_parser("remote-upgrade", help="allow upgrades requested from the dashboard")
    ru.add_argument("state", choices=["on", "off"])
    ru.set_defaults(fn=cmd_remote_upgrade)
    u = sub.add_parser("uninstall", help="remove the agent from this server")
    u.add_argument("--purge", action="store_true", help="also delete config and the agent user")
    u.add_argument("-y", "--yes", action="store_true")
    u.set_defaults(fn=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    # Everything after `--` is the action's command, verbatim. Split it off before argparse,
    # whose REMAINDER would otherwise swallow options like --sudo given after the name.
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1 :]
    opts = build_parser().parse_args(argv)
    opts.command = command
    try:
        opts.fn(opts)
    except Fail as exc:
        bad(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
