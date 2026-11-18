"""Rough location from a device's internet address, for computers that can't say where
they are themselves (a Mac's agent, or location services switched off).

Uses DB-IP's free "IP to City Lite" database (CC BY 4.0, https://db-ip.com), downloaded
by the relay process about once a month and kept next to the SQLite database. Lookups
are local: no device address is ever sent to an outside service. City-level only: it
can say which town, not which house, and a VPN shows the VPN's city.
"""

import gzip
import ipaddress
import logging
import os
import tempfile
import threading
import time
import urllib.request
from datetime import date
from functools import lru_cache
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 7 * 24 * 3600  # monthly releases; check weekly so a new month lands soon
FETCH_TIMEOUT = 60
MAX_DOWNLOAD = 300 * 1024 * 1024  # the city database is ~130 MB unpacked
# What "within" means for a city-level fix, shown on the device page.
ACCURACY_M = 25_000

_refreshing = threading.Lock()


def path() -> Path:
    return Path(settings.SQLITE_PATH).parent / "geoip-city.mmdb"


def _urls(today: date) -> list[str]:
    """This month's file, then last month's (a new month's file appears a day or two late)."""
    template = settings.DEVICE_GEOIP_URL
    if not template:
        return []
    last = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
    return [template.format(month=f"{d:%Y-%m}") for d in (today, last)]


def refresh(force: bool = False) -> bool:
    """Download a newer database if the copy on disk is old. Never raises; keeps the last
    good copy on any failure. Returns True when a new file was installed."""
    if not settings.DEVICE_GEOIP_URL or not _refreshing.acquire(blocking=False):
        return False
    try:
        target = path()
        if not force and target.exists() and time.time() - target.stat().st_mtime < REFRESH_SECONDS:
            return False
        for url in _urls(date.today()):
            try:
                if _download(url, target):
                    logger.info("IP location database updated from %s", url)
                    return True
            except Exception as exc:  # noqa: BLE001 - try the next month, keep the old copy
                logger.warning("IP location database: %s: %s", url, exc)
        if target.exists():
            target.touch()  # don't retry every hour while the source is down
        return False
    finally:
        _refreshing.release()


def refresh_in_background() -> None:
    """The relay's scheduler calls this hourly: a 60 MB download must not hold it up."""
    threading.Thread(target=refresh, name="geoip-refresh", daemon=True).start()


def _download(url: str, target: Path) -> bool:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")
    os.close(fd)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nexguard-dashboard"})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:  # noqa: S310
            stream = gzip.GzipFile(fileobj=response) if url.endswith(".gz") else response
            with open(tmp, "wb") as out:
                copied = 0
                while chunk := stream.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_DOWNLOAD:
                        raise ValueError("larger than expected")
                    out.write(chunk)
        import maxminddb

        with maxminddb.open_database(tmp) as db:  # a broken file never replaces a good one
            if "City" not in db.metadata().database_type:
                raise ValueError(f"not a city database: {db.metadata().database_type}")
        os.replace(tmp, target)
        _reader.cache_clear()
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)


@lru_cache(maxsize=1)
def _reader(mtime: float):
    import maxminddb

    return maxminddb.open_database(str(path()), mode=maxminddb.MODE_MMAP)


def locate(ip: str | None) -> dict | None:
    """{"lat", "lon", "accuracy", "city"} for a public address, or None (private, unknown,
    or no database yet)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not addr.is_global:
        return None  # home network, localhost, VPN-internal: says nothing about where
    target = path()
    if not target.exists():
        return None
    try:
        record = _reader(target.stat().st_mtime).get(str(addr))
    except Exception:  # noqa: BLE001 - a lookup must never break a check-in
        logger.exception("IP location lookup failed")
        return None
    loc = (record or {}).get("location") or {}
    if "latitude" not in loc or "longitude" not in loc:
        return None
    city = ((record.get("city") or {}).get("names") or {}).get("en", "")
    return {"lat": loc["latitude"], "lon": loc["longitude"], "accuracy": ACCURACY_M,
            "city": city}  # fmt: skip
