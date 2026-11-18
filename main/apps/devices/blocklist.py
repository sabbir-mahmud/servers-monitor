"""The hosts-file blocklist agents apply on children's and staff computers.

Three parts, merged per device by `for_device`:
- the adult-site list (ADULT_BLOCKLIST_URL, about 77k domains), refreshed at most daily by
  the relay process and kept on disk next to the database; a failed fetch keeps the last
  good copy;
- SafeSearch: search and video sites pointed at their "safe" addresses, resolved when the
  list is refreshed (Google, Bing and DuckDuckGo publish these for exactly this use);
- the parent's own blocked and allowed sites.

Blocking by hosts file can't see private DNS, VPNs or another browser's built-in DNS, so
`use-application-dns.net` is always blackholed: Firefox treats that as "this network
filters, don't switch to DNS over HTTPS on your own".
"""

import hashlib
import json
import logging
import os
import socket
import tempfile
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from . import domains

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 24 * 3600
FETCH_TIMEOUT = 30
MAX_DOWNLOAD = 20 * 1024 * 1024
BLACKHOLE = "0.0.0.0"

# Name -> the address its operator serves SafeSearch-only results from.
SAFE_SEARCH = {
    "forcesafesearch.google.com": ["www.google.com", "google.com"],
    "restrict.youtube.com": [
        "www.youtube.com",
        "m.youtube.com",
        "youtubei.googleapis.com",
        "youtube.googleapis.com",
        "www.youtube-nocookie.com",
    ],
    "strict.bing.com": ["www.bing.com", "bing.com"],
    "safe.duckduckgo.com": ["duckduckgo.com", "www.duckduckgo.com"],
}
ALWAYS = ["use-application-dns.net"]


def _dir() -> Path:
    return Path(settings.SQLITE_PATH).parent


def adult_path() -> Path:
    return _dir() / "adult-blocklist.txt"


def safe_search_path() -> Path:
    return _dir() / "safe-search.json"


def _write_atomic(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_hosts(text: str) -> list[str]:
    """Domains from a hosts-format list ("0.0.0.0 example.com", comments, "localhost"…)."""
    out = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
            out += [d for d in map(domains.normalize, parts[1:]) if d]
    return list(dict.fromkeys(out))


def refresh(force: bool = False) -> bool:
    """Fetch the adult list and resolve the SafeSearch addresses if they're over a day old.
    True when something was refreshed. Never raises: the relay calls it in its loop."""
    path = adult_path()
    if not force and path.exists() and time.time() - path.stat().st_mtime < REFRESH_SECONDS:
        return False
    refreshed = _refresh_safe_search()
    url = settings.ADULT_BLOCKLIST_URL
    if not url:
        return refreshed
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nexguard-blocklist"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 (setting)
            raw = resp.read(MAX_DOWNLOAD + 1)
        if len(raw) > MAX_DOWNLOAD:
            raise ValueError(f"over {MAX_DOWNLOAD} bytes")
        found = parse_hosts(raw.decode("utf-8", "replace"))
        if len(found) < 100:  # an error page, not a list: keep the last good copy
            raise ValueError(f"only {len(found)} domains")
    except (OSError, ValueError) as exc:
        logger.warning("Adult blocklist not refreshed (%s); keeping the last copy", exc)
        if path.exists():
            path.touch()  # try again tomorrow, not every loop
        return refreshed
    _write_atomic(path, "\n".join(found) + "\n")
    logger.info("Adult blocklist refreshed: %d domains", len(found))
    return True


def _refresh_safe_search() -> bool:
    resolved = {}
    for name in SAFE_SEARCH:
        try:
            resolved[name] = socket.getaddrinfo(name, 443, socket.AF_INET)[0][4][0]
        except OSError as exc:
            logger.warning("Couldn't resolve %s for SafeSearch: %s", name, exc)
    if not resolved:
        return False
    old = _read_safe_search()
    _write_atomic(safe_search_path(), json.dumps({**old, **resolved}, sort_keys=True))
    return True


def _read_safe_search() -> dict:
    try:
        return json.loads(safe_search_path().read_text())
    except OSError, ValueError:
        return {}


@lru_cache(maxsize=2)
def _adult(mtime: float) -> tuple[tuple[str, ...], str]:
    del mtime  # the cache key: a refresh rewrites the file
    try:
        text = adult_path().read_text(encoding="utf-8")
    except OSError:
        return (), ""
    return tuple(text.split()), hashlib.sha256(text.encode()).hexdigest()[:16]


def adult() -> tuple[tuple[str, ...], str]:
    """(domains, version) of the adult list on disk; empty before the first fetch."""
    try:
        mtime = adult_path().stat().st_mtime
    except OSError:
        return (), ""
    return _adult(mtime)


def version(controls) -> str:
    """Changes whenever the device's hosts lines would: the agent refetches only then."""
    if controls is None or not controls.blocking:
        return ""
    parts = [
        adult()[1] if controls.block_adult else "",
        json.dumps(_read_safe_search(), sort_keys=True) if controls.safe_search else "",
        controls.blocked_domains,
        controls.allowed_domains,
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def for_device(controls) -> list[str]:
    """The hosts lines ("ip domain") for this device's controls; [] when nothing blocks."""
    if controls is None or not controls.blocking:
        return []
    allowed = set(_with_www(domains.parse_list(controls.allowed_domains)))
    lines = [f"{BLACKHOLE} {d}" for d in ALWAYS]
    if controls.safe_search:
        for target, ip in sorted(_read_safe_search().items()):
            lines += [f"{ip} {d}" for d in SAFE_SEARCH.get(target, []) if d not in allowed]
    blocked = list(adult()[0]) if controls.block_adult else []
    blocked += _with_www(domains.parse_list(controls.blocked_domains))
    seen = set(ALWAYS)
    for d in blocked:
        if d not in allowed and d not in seen:
            seen.add(d)
            lines.append(f"{BLACKHOLE} {d}")
    return lines


def _with_www(names: list[str]) -> list[str]:
    """A hosts file has no wildcards: "example.com" must also cover "www.example.com"."""
    out = []
    for d in names:
        out.append(d)
        if not d.startswith("www."):
            out.append("www." + d)
    return out
