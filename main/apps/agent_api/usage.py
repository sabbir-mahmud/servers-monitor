"""Redis (Upstash) command usage: counted, projected and priced for the System page.

The dashboard counts its own commands exactly (every call on the relay client). Commands
agents send can't be seen from here, so they're estimated from what arrives: each message
is one RPUSH, a heartbeat also implies the agent's BLPOP round, and every 50th send an LTRIM.
"""

import calendar
import threading
from datetime import date

from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

_lock = threading.Lock()
_pending = {"dashboard": 0, "agent": 0.0}

AGENT_COST = {"hb": 2.0}  # RPUSH + the BLPOP that woke for it
AGENT_DEFAULT_COST = 1.0
TRIM_SHARE = 1 / 50


def count_dashboard(n: int = 1) -> None:
    with _lock:
        _pending["dashboard"] += n


def count_agent_message(kind) -> None:
    with _lock:
        _pending["agent"] += AGENT_COST.get(kind, AGENT_DEFAULT_COST) + TRIM_SHARE


def flush(today: date | None = None) -> None:
    """Add what was counted since the last flush to today's row (relay scheduler, every minute)."""
    from apps.core.models import RelayUsage

    with _lock:
        dash, agent = _pending["dashboard"], int(round(_pending["agent"]))
        _pending["dashboard"], _pending["agent"] = 0, _pending["agent"] - agent
    if not dash and not agent:
        return
    day = today or timezone.localdate()
    row, created = RelayUsage.objects.get_or_create(
        day=day, defaults={"dashboard": dash, "agent": agent}
    )
    if not created:
        RelayUsage.objects.filter(pk=row.pk).update(
            dashboard=F("dashboard") + dash, agent=F("agent") + agent
        )


def month_summary(today: date | None = None) -> dict | None:
    from apps.core.models import RelayUsage

    today = today or timezone.localdate()
    rows = RelayUsage.objects.filter(day__year=today.year, day__month=today.month)
    totals = rows.aggregate(d=Sum("dashboard"), a=Sum("agent"))
    if totals["d"] is None:
        return None
    so_far = totals["d"] + totals["a"]
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Upstash bills the whole month, so project the daily average over the days counted
    # (a meter that started mid-month averages over fewer days, it doesn't read low).
    first = rows.order_by("day").values_list("day", flat=True).first()
    days_counted = max((today - first).days + 1, 1)
    projected = so_far / days_counted * days_in_month
    free = settings.RELAY_FREE_COMMANDS
    cost = max(projected - free, 0) / 100_000 * settings.RELAY_PRICE_PER_100K
    return {
        "so_far": so_far,
        "dashboard": totals["d"],
        "agent": totals["a"],
        "projected": int(projected),
        "free": free,
        "cost": round(cost, 2),
        "warn": projected > free * 0.8,
    }


def prune(keep_days: int = 400) -> int:
    from datetime import timedelta

    from apps.core.models import RelayUsage

    return RelayUsage.objects.filter(
        day__lt=timezone.localdate() - timedelta(days=keep_days)
    ).delete()[0]
