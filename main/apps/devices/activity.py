"""Screen time and websites from the agent, and what the device page shows of them.

The agent sends what it counted since its last successful check-in, as a batch with an
id. Counters only ever add, so a batch resent after a lost answer is recognised by its id
and skipped. Nothing is stored unless the device may have controls (child or staff, with
the agent) and the matching switch is on: an agent that keeps sending after a parent
turned it off, or a modified one, changes nothing.
"""

import uuid
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from . import domains
from .models import ActivityBatch, Device, DeviceControls, ScreenTime, WebActivity

MAX_ROWS = 2000  # per batch, screen and web together (the agent sends at most 500)
DAY_SECONDS = 86400


def controls_for(device: Device) -> DeviceControls | None:
    try:
        return device.controls
    except DeviceControls.DoesNotExist:
        return None


def apply(device: Device, activity) -> None:
    """Store one batch. Bad rows are dropped, never the check-in."""
    if not isinstance(activity, dict) or not device.controls_allowed:
        return
    controls = controls_for(device)
    if controls is None or not (controls.screen_time or controls.web_activity):
        return
    try:
        batch = uuid.UUID(str(activity.get("batch")))
    except ValueError:
        return
    screen = _screen_rows(activity.get("screen")) if controls.screen_time else {}
    web = _web_rows(activity.get("web")) if controls.web_activity else {}
    try:
        with transaction.atomic():
            ActivityBatch.objects.create(device=device, batch_id=batch)
            _add(device, ScreenTime, screen, ("day", "os_user", "app"), ("seconds",))
            _add(device, WebActivity, web, ("day", "os_user", "browser", "domain"),
                 ("visits", "seconds"))  # fmt: skip
    except IntegrityError:
        return  # this batch was already counted


def _rows(raw) -> list[dict]:
    return [r for r in raw[:MAX_ROWS] if isinstance(r, dict)] if isinstance(raw, list) else []


def _day(value) -> date | None:
    """Days the agent can legitimately report: within retention, not in the future
    (one day of slack for time zones)."""
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return None
    today = timezone.localdate()
    oldest = today - timedelta(days=settings.DEVICE_ACTIVITY_DAYS)
    return day if oldest <= day <= today + timedelta(days=1) else None


def _count(value, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return min(int(value), high)


def _label(value, length: int) -> str:
    return " ".join(str(value).split())[:length] if isinstance(value, str) else ""


def _screen_rows(raw) -> dict:
    out = defaultdict(lambda: [0])
    for r in _rows(raw):
        day, seconds = _day(r.get("day")), _count(r.get("seconds"), DAY_SECONDS)
        user, app = _label(r.get("user"), 100), _label(r.get("app"), 100) or "Unknown"
        if day and seconds and user:
            out[(day, user, app)][0] += seconds
    return out


def _web_rows(raw) -> dict:
    out = defaultdict(lambda: [0, 0])
    for r in _rows(raw):
        day, domain = _day(r.get("day")), domains.normalize(r.get("domain"))
        user, browser = _label(r.get("user"), 100), _label(r.get("browser"), 50) or "Browser"
        visits = _count(r.get("visits"), 100_000) or 0
        seconds = _count(r.get("seconds"), DAY_SECONDS) or 0
        if day and domain and user and (visits or seconds):
            row = out[(day, user, browser, domain)]
            row[0] += visits
            row[1] += seconds
    return out


def _add(device, model, rows: dict, keys: tuple, counters: tuple) -> None:
    """Add counters into existing rows and create the rest: one read, one bulk update,
    one bulk insert, however many rows. Inside the batch's transaction."""
    if not rows:
        return
    existing = {
        tuple(getattr(obj, k) for k in keys): obj
        for obj in model.objects.filter(device=device, day__in={k[0] for k in rows})
    }
    changed, new = [], []
    for key, values in rows.items():
        obj = existing.get(key)
        if obj is None:
            new.append(model(device=device, **dict(zip(keys, key, strict=True)),
                             **dict(zip(counters, values, strict=True))))  # fmt: skip
            continue
        for field, value in zip(counters, values, strict=True):
            total = getattr(obj, field) + value
            setattr(obj, field, min(total, DAY_SECONDS) if field == "seconds" else total)
        changed.append(obj)
    if changed:
        model.objects.bulk_update(changed, counters)
    if new:
        model.objects.bulk_create(new)


def prune(batch_size: int = 5000) -> int:
    cutoff = timezone.localdate() - timedelta(days=settings.DEVICE_ACTIVITY_DAYS)
    total = 0
    for model, field, value in (
        (ScreenTime, "day__lt", cutoff),
        (WebActivity, "day__lt", cutoff),
        (ActivityBatch, "received_at__lt", timezone.now() - timedelta(days=2)),
    ):
        while ids := list(
            model.objects.filter(**{field: value}).values_list("pk", flat=True)[:batch_size]
        ):
            total += model.objects.filter(pk__in=ids).delete()[0]
    return total


# ---------------------------------------------------------------- what the page shows


def summary(device: Device, day: date) -> dict:
    """Screen time for the day and the week before it, top apps and websites that day."""
    week = [day - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = dict(
        ScreenTime.objects.filter(device=device, day__in=week)
        .values("day")
        .annotate(total=Sum("seconds"))
        .values_list("day", "total")
    )
    longest = max(per_day.values(), default=0)
    apps = list(
        ScreenTime.objects.filter(device=device, day=day)
        .values("app")
        .annotate(seconds=Sum("seconds"))
        .order_by("-seconds")[:12]
    )
    sites = list(
        WebActivity.objects.filter(device=device, day=day)
        .values("domain")
        .annotate(visits=Sum("visits"), seconds=Sum("seconds"))
        .order_by("-seconds", "-visits")[:50]
    )
    top = max((a["seconds"] for a in apps), default=0)
    for a in apps:
        a["share"] = round(100 * a["seconds"] / top) if top else 0
    return {
        "day": day,
        "total": per_day.get(day, 0),
        "week": [
            {
                "day": d,
                "seconds": per_day.get(d, 0),
                "share": round(100 * per_day.get(d, 0) / longest) if longest else 0,
            }
            for d in week
        ],  # fmt: skip
        "apps": apps,
        "sites": sites,
        "users": list(
            ScreenTime.objects.filter(device=device, day=day)
            .values_list("os_user", flat=True)
            .distinct()
        ),
    }
