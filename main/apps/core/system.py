"""The dashboard watching itself.

- Watchdog: the relay process pings WATCHDOG_URL (e.g. a Healthchecks.io check) every
  minute. If the dashboard machine sleeps, crashes or loses its network, the pings stop and
  that external service alerts you: the one failure the dashboard can't report on its own.
- Downtime: when the relay process starts after a gap, it tells every notification channel
  how long monitoring was blind, and records it in the audit log.
"""

import logging
import time
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import netguard
from .models import SystemState

logger = logging.getLogger(__name__)

WATCHDOG_EVERY = 60
DOWNTIME_THRESHOLD = timedelta(minutes=5)


def ping_watchdog() -> dict | None:
    """One ping. Returns the recorded state, or None when no watchdog is configured."""
    if not settings.WATCHDOG_URL:
        return None
    started = time.perf_counter()
    state = {"at": timezone.now().isoformat()}
    try:
        request = urllib.request.Request(
            settings.WATCHDOG_URL, headers={"User-Agent": "ServerMonitor/watchdog"}
        )
        with netguard.urlopen(request, timeout=10) as resp:
            resp.read(256)
            state |= {"ok": True, "status": resp.status}
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        state |= {"ok": False, "error": str(reason)[:200]}
        logger.warning("Watchdog ping failed: %s", reason)
    state["ms"] = int((time.perf_counter() - started) * 1000)
    SystemState.put("watchdog", state)
    return state


def check_downtime(last_alive_at, now=None) -> timedelta | None:
    """Called once when the relay process starts, with the previous liveness marker time.
    Reports a gap longer than DOWNTIME_THRESHOLD; returns it (or None)."""
    if last_alive_at is None:
        return None
    now = now or timezone.now()
    gap = now - last_alive_at
    if gap < DOWNTIME_THRESHOLD:
        return None
    from apps.alerts.delivery import send_system
    from apps.audit.services import record

    minutes = int(gap.total_seconds() // 60)
    start, end = timezone.localtime(last_alive_at), timezone.localtime(now)
    span = (
        f"{start:%b %d %H:%M} to {end:%H:%M %Z}"
        if start.date() == end.date()
        else (f"{start:%b %d %H:%M} to {end:%b %d %H:%M %Z}")
    )
    duration = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
    record(None, "system.downtime", target=f"{minutes} min", started=last_alive_at.isoformat(),
           ended=now.isoformat())  # fmt: skip
    SystemState.put(
        "last_downtime",
        {"from": last_alive_at.isoformat(), "to": now.isoformat(), "minutes": minutes},
    )
    send_system(
        f"{settings.SITE_NAME} was offline from {span} ({duration}). "
        "Checks and alerts during that time were missed; it's monitoring again now."
    )
    return gap
