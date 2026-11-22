#!/usr/bin/env python3
"""NexGuard device client for computers (macOS, Windows, Linux): the reference client.

Pairs this computer with a NexGuard account using the code the parent (or admin) got on the
dashboard, then checks in every few minutes with battery, storage and the OS version.
It is visible by design: it says who will see what before pairing, and `status` shows
which account is watching.

    python3 nexguard_device.py pair                # asks for the relay address and the code
    python3 nexguard_device.py run                 # check in forever (run it at login)
    python3 nexguard_device.py status
    python3 nexguard_device.py unpair              # forget the pairing on this computer

It never talks to the dashboard: like the Go agent (device-agent/internal/api), it pairs
and checks in through the dashboard's Redis relay. The relay address is in the line the
dashboard's connect page shows (RELAY='rediss://sm-pc:…'). Standard library only; battery
needs `psutil` if installed.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

VERSION = "0.2.0"
TIMEOUT = 15
PAIR_WAIT = 60  # seconds to wait for the dashboard's answer to a pairing request


class ClientError(Exception):
    pass


class Unpaired(ClientError):
    """The relay no longer takes this computer's login: removed, or paired again."""


# --------------------------------------------------------------------------- config


def config_path() -> Path:
    if override := os.environ.get("NEXGUARD_DEVICE_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "NexGuard" / "device.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NexGuard" / "device.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "nexguard" / "device.json"


def load_config() -> dict | None:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return None


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # The relay login and signing key are credentials: readable by this user only.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def forget() -> bool:
    try:
        config_path().unlink()
        return True
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- facts


def this_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def os_version() -> str:
    kind = this_platform()
    if kind == "macos":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if kind == "windows":
        return f"Windows {platform.release()} ({platform.version()})"
    try:
        info = platform.freedesktop_os_release()
        return info.get("PRETTY_NAME") or f"Linux {platform.release()}"
    except OSError:
        return f"Linux {platform.release()}"


def battery() -> dict | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        b = psutil.sensors_battery()
    except Exception:  # some platforms raise instead of returning None
        return None
    if b is None:
        return None  # a desktop with no battery
    return {"percent": round(b.percent), "charging": bool(b.power_plugged)}


def storage() -> dict | None:
    root = os.environ.get("SystemDrive", "C:") + "\\" if sys.platform == "win32" else "/"
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    return {"total": usage.total, "free": usage.free}


def _run(*cmd: str) -> str:
    """One line of output from a quick system command, or "" (never raises)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""


def model() -> str:
    """What the parent would call it ("Mac · Apple M3", "Dell XPS 13 9340"), not the CPU
    architecture. Falls back to the architecture only when nothing better is known."""
    if sys.platform == "darwin":
        chip = _run("sysctl", "-n", "machdep.cpu.brand_string")
        return f"Mac · {chip}" if chip else "Mac"
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"
            ) as k:
                maker = winreg.QueryValueEx(k, "SystemManufacturer")[0].strip()
                name = winreg.QueryValueEx(k, "SystemProductName")[0].strip()
            return " ".join(p for p in (maker, name) if p and p not in name)[:100] or name
        except OSError:
            return platform.machine()
    try:
        name = Path("/sys/devices/virtual/dmi/id/product_name").read_text().strip()
        vendor = Path("/sys/devices/virtual/dmi/id/sys_vendor").read_text().strip()
    except OSError:
        return platform.machine()
    junk = {"", "To Be Filled By O.E.M.", "System Product Name", "Default string"}
    if name in junk:
        return platform.machine()
    return (name if not vendor or vendor in name or vendor in junk else f"{vendor} {name}")[:100]


def short_hostname(name: str) -> str:
    """'5-1-1-3.ipv4.isp.example' (a reverse-DNS name some networks hand out) or
    'sara-mbp.local' → the first label only."""
    first = name.split(".")[0]
    return first if first and not first.replace("-", "").isdigit() else name


def device_name() -> str:
    """The name the owner gave the computer (macOS "Sara's MacBook Air"), else the host name."""
    if sys.platform == "darwin" and (name := _run("scutil", "--get", "ComputerName")):
        return name[:100]
    return short_hostname(socket.gethostname())[:100]


def device_info() -> dict:
    return {
        "platform": this_platform(),
        "os_version": os_version(),
        "model": model(),
        "hostname": device_name(),
        "app_version": f"nexguard-device {VERSION}",
    }


def report() -> dict:
    data = device_info()
    if (b := battery()) is not None:
        data["battery"] = b
    if (s := storage()) is not None:
        data["storage"] = s
    return data


# --------------------------------------------------------------------------- relay


class RedisError(ClientError):
    """An error reply from the relay ("WRONGPASS …", "NOPERM …")."""


class Redis:
    """Just enough of RESP2: AUTH, RPUSH and GET, over TLS. One instance, one connection."""

    def __init__(self, url: str):
        u = urllib.parse.urlsplit(url)
        self.host, self.port = u.hostname, u.port or 6379
        self.user = urllib.parse.unquote(u.username or "")
        self.password = urllib.parse.unquote(u.password or "")
        self.tls = u.scheme == "rediss"
        self.sock = self.reader = None

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.sock = self.reader = None

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=TIMEOUT)
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except OSError as exc:
            raise ClientError(f"Can't reach the relay at {self.host}: {exc}") from None
        self.sock, self.reader = sock, sock.makefile("rb")
        if self.password:
            self.call(
                *(["AUTH", self.user, self.password] if self.user else ["AUTH", self.password])
            )

    def call(self, *args: str):
        if self.sock is None:
            self._connect()
        out = [b"*%d\r\n" % len(args)]
        for arg in args:
            data = arg.encode()
            out.append(b"$%d\r\n%s\r\n" % (len(data), data))
        try:
            self.sock.sendall(b"".join(out))
            return self._read()
        except OSError as exc:
            self.close()
            raise ClientError(f"Lost the relay: {exc}") from None

    def _read(self):
        line = self.reader.readline()
        if not line:
            raise OSError("the relay closed the connection")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode(errors="replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self.reader.read(n + 2)[:-2].decode()
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise OSError(f"unexpected reply {line[:20]!r}")


def _denied(exc: RedisError) -> bool:
    text = str(exc)
    return text.startswith(("WRONGPASS", "NOPERM")) or "invalid username-password" in text


def check_relay(url: str) -> str:
    """The relay address from the dashboard. Plain redis:// only to this machine or a
    private network (the login would cross the internet in the clear otherwise)."""
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("redis", "rediss") or not parts.hostname:
        raise ClientError("Not a relay address: copy RELAY='rediss://…' from the dashboard's line.")
    if parts.scheme == "redis" and not _is_local(parts.hostname):
        if os.environ.get("NEXGUARD_ALLOW_PLAIN") != "1":
            raise ClientError("Use the rediss:// (encrypted) relay address.")
    return url


def _is_local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def sign(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def ask_dashboard(relay_url: str, payload: dict, *, wait: float = PAIR_WAIT,
                  sleep=time.sleep) -> dict:  # fmt: skip
    """One pairing request through the relay's pairing inbox; returns the answer."""
    relay_url = check_relay(relay_url)
    prefix = urllib.parse.parse_qs(urllib.parse.urlsplit(relay_url).query).get("prefix", [""])[0]
    if not prefix:
        raise ClientError("The relay address has no ?prefix=: copy the whole line.")
    nonce = secrets.token_hex(16)
    body = json.dumps({**payload, "at": int(time.time()), "nonce": nonce})
    conn = Redis(relay_url)
    try:
        conn.call("RPUSH", f"{prefix}:pair:in", json.dumps({"t": "pair", "body": body}))
        deadline = time.monotonic() + wait
        while True:
            raw = conn.call("GET", f"{prefix}:pair:out:{nonce}")
            if raw:
                answer = json.loads(raw)
                if answer.get("state") == "error":
                    raise ClientError(answer.get("message") or "The dashboard refused.")
                return answer
            if time.monotonic() > deadline:
                raise ClientError("The dashboard didn't answer through the relay. Is its relay "
                                  "process running? The code still works.")  # fmt: skip
            sleep(0.5)
    except RedisError as exc:
        if _denied(exc):
            raise ClientError("The relay refused this pairing address: get a new line.") from None
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- commands


def pair(relay_url: str, code: str, *, accept=None, ask=ask_dashboard) -> dict:
    """Pair and save this computer's relay login. `accept(disclosure) -> bool` must agree
    before anything is stored; the default asks on the terminal."""
    info = device_info()
    # Ask before pairing: a preview doesn't use up the code, so declining changes nothing.
    preview = ask(relay_url, {"code": code, "info": info, "preview": True})
    accept = accept or _ask
    if not accept(preview.get("disclosure", "")):
        raise ClientError("Not paired: you declined. The code still works if you change your mind.")
    key = secrets.token_hex(32)
    result = ask(relay_url, {"code": code, "info": info, "secret": key})
    login = result.get("relay") or {}
    if result.get("state") != "paired" or not login.get("url") or not login.get("inbox"):
        raise ClientError("The dashboard sent an unexpected answer.")
    data = {
        "relay": login,
        "key": key,
        "device": result.get("device", {}),
        "checkin_seconds": int(result.get("checkin_seconds") or 300),
    }
    save_config(data)
    return data


def _ask(disclosure: str) -> bool:
    print(f"\n{disclosure}\n")
    return input("Allow this? [y/N] ").strip().lower() in ("y", "yes")


def checkin(config: dict) -> None:
    """One signed check-in, then the settings the dashboard left (its interval)."""
    login, key = config["relay"], bytes.fromhex(config["key"])
    body = json.dumps({"at": int(time.time()), "report": report()})
    conn = Redis(login["url"])
    try:
        conn.call("RPUSH", login["inbox"], json.dumps({"t": "ci", "body": body,
                                                       "sig": sign(key, body)}))  # fmt: skip
        raw = conn.call("GET", login["config"]) if login.get("config") else None
    except RedisError as exc:
        if _denied(exc):
            raise Unpaired(str(exc)) from None
        raise
    finally:
        conn.close()
    if raw:
        sealed = json.loads(raw)
        if hmac.compare_digest(sealed.get("sig", ""), sign(key, sealed.get("body", ""))):
            seconds = json.loads(sealed["body"]).get("checkin_seconds")
            if isinstance(seconds, int) and seconds >= 60:
                config["checkin_seconds"] = seconds


def run(config: dict, *, once=False, sleep=time.sleep, send=None) -> int:
    send = send or checkin
    failures = 0
    while True:
        try:
            send(config)
            failures = 0
        except Unpaired as exc:
            forget()
            print(f"This computer isn't paired any more ({exc}). Pair it again to resume.",
                  file=sys.stderr)  # fmt: skip
            return 3
        except ClientError as exc:
            failures += 1
            print(f"Check-in failed: {exc}", file=sys.stderr)
        if once:
            return 0 if failures == 0 else 1
        # Back off on errors (relay down, offline laptop), up to 30 minutes.
        delay = config.get("checkin_seconds", 300) * (2 ** min(failures, 3))
        sleep(min(delay, 1800))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NexGuard device client for computers")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair this computer using the code from the dashboard")
    p.add_argument("--relay", help="relay address from the dashboard's line, rediss://sm-pc:…")
    p.add_argument("--code", help="the pairing code, e.g. K7PQ-3MXA")
    p.add_argument("--yes", action="store_true", help="accept the disclosure without asking")
    r = sub.add_parser("run", help="check in now and then every few minutes")
    r.add_argument("--once", action="store_true", help="check in once and exit")
    sub.add_parser("status", help="show the pairing")
    sub.add_parser("unpair", help="forget the pairing on this computer")
    args = parser.parse_args(argv)

    try:
        if args.command == "pair":
            relay_url = args.relay or input("Relay address (RELAY='…' in the dashboard's line): ")
            code = args.code or input("Pairing code: ")
            data = pair(relay_url, code, accept=(lambda _d: True) if args.yes else None)
            device = data["device"]
            print(f"Paired as \"{device.get('name')}\" in {device.get('account')}. "
                  "Run `nexguard_device.py run` to start checking in.")  # fmt: skip
            return 0
        config = load_config()
        if config is not None and "relay" not in config:  # paired over the old web API
            forget()
            config = None
            print("This computer was paired with an older NexGuard: pair it again.",
                  file=sys.stderr)  # fmt: skip
        if args.command == "unpair":
            print("Pairing forgotten." if forget() else "This computer wasn't paired.")
            return 0
        if config is None:
            print("Not paired. Run `nexguard_device.py pair` first.", file=sys.stderr)
            return 2
        if args.command == "status":
            device = config.get("device", {})
            host = urllib.parse.urlsplit(config["relay"]["url"]).hostname
            print(f"Paired with {device.get('account')} through the relay at {host} as "
                  f"\"{device.get('name')}\"; checks in every {config['checkin_seconds']} s.")  # fmt: skip
            return 0
        return run(config, once=args.once)
    except ClientError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
