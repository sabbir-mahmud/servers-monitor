"""Turn a StatusPage into what gets published: a snapshot dict (the only data that leaves
this dashboard), a self-contained index.html, and status.json."""

import hashlib
import io
import json
import zipfile
from datetime import timedelta

from django.template.loader import render_to_string
from django.utils import timezone

from apps.monitors.models import MonitorStatus
from apps.monitors.stats import daily_uptime

from .models import Impact, StatusPage

DAYS = 90
INCIDENT_HISTORY = timedelta(days=14)

STATE = {
    MonitorStatus.UP: "operational",
    MonitorStatus.DEGRADED: "degraded",
    MonitorStatus.DOWN: "outage",
    MonitorStatus.PENDING: "unknown",
    MonitorStatus.PAUSED: "unknown",
}
STATE_LABEL = {"operational": "Operational", "degraded": "Degraded performance", "outage": "Outage",
               "unknown": "No data", "maintenance": "Under maintenance"}  # fmt: skip


def _overall(components, incidents) -> tuple[str, str]:
    states = [c["state"] for c in components]
    if states and all(s == "outage" for s in states):
        return "major", "Major outage"
    if "outage" in states:
        return "partial", "Partial outage"
    if any(i["impact"] == Impact.MAJOR for i in incidents):
        return "partial", "Major incident in progress"
    if "degraded" in states or any(i["impact"] == Impact.MINOR for i in incidents):
        return "degraded", "Degraded performance"
    if any(i["impact"] == Impact.MAINTENANCE for i in incidents):
        return "maintenance", "Scheduled maintenance"
    return "operational", "All systems operational"


def snapshot(page: StatusPage, now=None) -> dict:
    """Everything the public page shows, and nothing else (allow-listed fields only)."""
    now = now or timezone.now()
    entries = list(page.entries.select_related("monitor"))
    uptime = daily_uptime([e.monitor_id for e in entries], DAYS) if page.show_uptime else {}
    today = timezone.localdate(now)
    days = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    active = list(
        page.incidents.filter(resolved_at__isnull=True).prefetch_related("updates", "affected")
    )
    maintenance_ids = {
        a.pk for i in active if i.impact == Impact.MAINTENANCE for a in i.affected.all()
    }

    components = []
    for e in entries:
        m = e.monitor
        state = "unknown" if not m.is_enabled else STATE.get(m.status, "unknown")
        if e.pk in maintenance_ids and state != "operational":
            state = "maintenance"
        comp = {"name": e.display_name, "state": state, "label": STATE_LABEL[state]}
        if page.show_uptime:
            per_day = uptime.get(m.pk, {})
            bars, up_total, all_total = [], 0, 0
            for d in days:
                up, total = per_day.get(d, (0, 0))
                up_total, all_total = up_total + up, all_total + total
                pct = round(100 * up / total, 2) if total else None
                level = (
                    "none"
                    if pct is None
                    else "up"
                    if pct >= 99.9
                    else "minor"
                    if pct >= 95
                    else "down"
                )
                bars.append({"date": d.isoformat(), "pct": pct, "level": level})
            comp["days"] = bars
            comp["uptime"] = round(100 * up_total / all_total, 2) if all_total else None
        components.append(comp)

    def incident(i):
        return {
            "title": i.title,
            "impact": i.impact,
            "status": i.get_status_display(),
            "started": i.created_at.isoformat(),
            "resolved": i.resolved_at.isoformat() if i.resolved_at else None,
            "affects": [a.display_name for a in i.affected.all()],
            "updates": [
                {
                    "status": u.get_status_display(),
                    "message": u.message,
                    "at": u.created_at.isoformat(),
                }
                for u in i.updates.all()
            ],  # fmt: skip
        }

    recent = page.incidents.filter(resolved_at__gte=now - INCIDENT_HISTORY).prefetch_related(
        "updates", "affected"
    )
    active_data = [incident(i) for i in active]
    level, label = _overall(components, active_data)
    return {
        "title": page.title,
        "description": page.description,
        "logo_url": page.logo_url,
        "homepage_url": page.homepage_url,
        "status": {"level": level, "label": label},
        "components": components,
        "active_incidents": active_data,
        "past_incidents": [incident(i) for i in recent],
        "days": DAYS if page.show_uptime else 0,
    }


def content_hash(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()


def render(page: StatusPage, now=None) -> tuple[dict, str, str]:
    """(snapshot, index.html, status.json)."""
    now = now or timezone.now()
    snap = snapshot(page, now)
    published = {**snap, "generated_at": now.isoformat()}
    html = render_to_string("status/public.html", {"s": snap, "generated_at": now})
    return snap, html, json.dumps(published, indent=2, default=str)


def as_zip(page: StatusPage) -> bytes:
    _, html, data = render(page)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", html)
        z.writestr("status.json", data)
    return buf.getvalue()
