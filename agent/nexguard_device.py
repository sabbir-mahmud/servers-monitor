#!/usr/bin/env python3
"""NexGuard device client for computers (macOS, Windows, Linux): the reference client.

Pairs this computer with a NexGuard account using the code the parent (or admin) got on the
dashboard, then checks in every few minutes with battery, storage and the OS version.
It is visible by design: it says who will see what before pairing, and `status` shows
which account is watching.

    python3 nexguard_device.py pair                # asks for the server and the code
    python3 nexguard_device.py run                 # check in forever (run it at login)
    python3 nexguard_device.py status
    python3 nexguard_device.py unpair              # forget the pairing on this computer

Standard library only; battery needs `psutil` if installed. The phone apps (Android, iOS)
speak the same API: POST /device/v1/pair and /device/v1/checkin.
"""

import argparse
import ipaddress
import json
import os
import platform
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
TIMEOUT = 15


class ClientError(Exception):
    pass


class Unpaired(ClientError):
    """The dashboard no longer knows this token: removed, or paired again elsewhere."""


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
    # The token is a credential: readable by this user only.
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


def device_info() -> dict:
    return {
        "platform": this_platform(),
        "os_version": os_version(),
        "model": platform.machine(),
        "hostname": socket.gethostname()[:100],
        "app_version": f"nexguard-device {VERSION}",
    }


def report() -> dict:
    data = device_info()
    if (b := battery()) is not None:
        data["battery"] = b
    if (s := storage()) is not None:
        data["storage"] = s
    return data


# --------------------------------------------------------------------------- http


def check_server(url: str) -> str:
    """Normalize the dashboard address. Plain HTTP only to this machine or a private
    network (the token would cross the internet in the clear otherwise)."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClientError(f"Not a web address: {url}")
    if parts.scheme == "http" and not _is_local(parts.hostname):
        if os.environ.get("NEXGUARD_ALLOW_HTTP") != "1":
            raise ClientError("Use the https:// address of the dashboard.")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _is_local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def post(url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": f"nexguard-device/{VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read() or b"{}").get("error") or exc.reason
        except ValueError:
            message = exc.reason
        if exc.code == 401 and token:
            raise Unpaired(message) from None
        raise ClientError(f"{message} (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClientError(f"Can't reach the dashboard: {getattr(exc, 'reason', exc)}") from None


# --------------------------------------------------------------------------- commands


def pair(server: str, code: str, *, accept=None) -> dict:
    """Pair and save the token. `accept(disclosure) -> bool` must agree before anything
    is stored; the default asks on the terminal."""
    server = check_server(server)
    result = post(f"{server}/device/v1/pair", {"code": code, **device_info()})
    disclosure = result.get("disclosure", "")
    accept = accept or _ask
    if not accept(disclosure):
        raise ClientError("Not paired: you declined. The code still works if you change your mind.")
    data = {
        "server": server,
        "token": result["token"],
        "device": result.get("device", {}),
        "checkin_seconds": int(result.get("checkin_seconds") or 300),
    }
    save_config(data)
    return data


def _ask(disclosure: str) -> bool:
    print(f"\n{disclosure}\n")
    return input("Allow this? [y/N] ").strip().lower() in ("y", "yes")


def checkin(config: dict) -> dict:
    result = post(f"{config['server']}/device/v1/checkin", report(), token=config["token"])
    if seconds := result.get("checkin_seconds"):
        config["checkin_seconds"] = int(seconds)
    return result


def run(config: dict, *, once=False, sleep=time.sleep) -> int:
    failures = 0
    while True:
        try:
            checkin(config)
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
        # Back off on errors (dashboard down, offline laptop), up to 30 minutes.
        delay = config.get("checkin_seconds", 300) * (2 ** min(failures, 3))
        sleep(min(delay, 1800))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NexGuard device client for computers")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair this computer using the code from the dashboard")
    p.add_argument("--server", help="dashboard address, e.g. https://nexguard.example.com")
    p.add_argument("--code", help="the pairing code, e.g. K7PQ-3MXA")
    p.add_argument("--yes", action="store_true", help="accept the disclosure without asking")
    r = sub.add_parser("run", help="check in now and then every few minutes")
    r.add_argument("--once", action="store_true", help="check in once and exit")
    sub.add_parser("status", help="show the pairing")
    sub.add_parser("unpair", help="forget the pairing on this computer")
    args = parser.parse_args(argv)

    try:
        if args.command == "pair":
            server = args.server or input("Dashboard address: ")
            code = args.code or input("Pairing code: ")
            data = pair(server, code, accept=(lambda _d: True) if args.yes else None)
            device = data["device"]
            print(f"Paired as \"{device.get('name')}\" in {device.get('account')}. "
                  "Run `nexguard_device.py run` to start checking in.")  # fmt: skip
            return 0
        config = load_config()
        if args.command == "unpair":
            print("Pairing forgotten." if forget() else "This computer wasn't paired.")
            return 0
        if config is None:
            print("Not paired. Run `nexguard_device.py pair` first.", file=sys.stderr)
            return 2
        if args.command == "status":
            device = config.get("device", {})
            print(f"Paired with {device.get('account')} at {config['server']} as "
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
