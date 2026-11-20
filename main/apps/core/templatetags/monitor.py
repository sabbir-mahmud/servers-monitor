"""Presentation helpers: status badges (icon + label, never colour alone), meters, and
server-rendered SVG sparklines that HTMX can swap without a chart library."""

import json

from django import template
from django.template.defaultfilters import pluralize
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.timesince import timesince

register = template.Library()

HEALTH_BADGES = {
    "online": ("good", "●", "Online"),
    "late": ("warning", "◐", "Late"),
    "offline": ("critical", "✕", "Offline"),
    "pending": ("muted", "○", "Awaiting agent"),
    "disabled": ("muted", "⏸", "Disabled"),
}
COMMAND_BADGES = {
    "pending": ("muted", "○", "Queued"),
    "running": ("info", "◔", "Running"),
    "succeeded": ("good", "✓", "Succeeded"),
    "failed": ("critical", "✕", "Failed"),
    "lost": ("serious", "?", "Lost"),
    "expired": ("muted", "⌛", "Expired"),
    "cancelled": ("muted", "–", "Cancelled"),
    "waiting": ("muted", "⋯", "Waiting"),
}
MONITOR_BADGES = {
    "up": ("good", "●", "Up"),
    "degraded": ("warning", "◐", "Slow"),
    "down": ("critical", "✕", "Down"),
    "pending": ("muted", "○", "Checking…"),
    "paused": ("muted", "⏸", "Paused"),
}
DEVICE_BADGES = {
    "online": ("good", "●", "Online"),
    "offline": ("critical", "✕", "Offline"),
    "waiting": ("muted", "○", "Waiting to pair"),
}
SERVICE_BADGES = {
    "up": ("good", "●", "Up"),
    "down": ("critical", "✕", "Down"),
    "unknown": ("muted", "?", "Unknown"),
}


def _badge(table, key):
    tone, icon, label = table.get(key, ("muted", "?", str(key).title()))
    return format_html(
        '<span class="status status-{}" data-state="{}"><span class="status-dot" aria-hidden="true">'
        "</span>{}</span>",
        tone,
        key,
        label,
    )


# Line icons drawn on a 24px grid with a 1.75 stroke; they inherit currentColor.
ICONS = {
    "server": '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
    "layers": '<path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    "play": '<path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
    "memory": '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10v4M10 10v4M14 10v4M18 10v4"/>',
    "disk": '<path d="M22 12H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><path d="M6 16h.01M10 16h.01"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "plug": '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0V8z"/><path d="M12 17v5"/>',
    "network": '<path d="m3 16 4 4 4-4M7 20V4M21 8l-4-4-4 4M17 4v16"/>',
    "arrow-left": '<path d="m12 19-7-7 7-7M19 12H5"/>',
    "copy": '<rect width="13" height="13" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
    "terminal": '<path d="m4 17 6-6-6-6M12 19h8"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "rocket": '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    "git-branch": '<path d="M6 3v12"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "git-commit": '<circle cx="12" cy="12" r="3"/><path d="M3 12h6M15 12h6"/>',
    "code": '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    "file": '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><path d="M14 2v6h6M16 13H8M16 17H8"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m9 12 2 2 4-4"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "edit": '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "key": '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
    "trash": '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    "chevron": '<path d="m9 18 6-6-6-6"/>',
    "chart": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 6-6"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><path d="m2 2 20 20"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "timer": '<path d="M10 2h4M12 14l3-3"/><circle cx="12" cy="14" r="8"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "bell-off": '<path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 .6 5"/><path d="M17 17H3s3-2 3-9a4.67 4.67 0 0 1 .3-1.7"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="m2 2 20 20"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
    '<path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6"/>',
    "phone": '<rect width="12" height="20" x="6" y="2" rx="2.5"/><path d="M11 18h2"/>',
    "laptop": '<rect width="16" height="11" x="4" y="4" rx="1.5"/><path d="M2 19h20"/>',
    "battery": '<rect width="16" height="10" x="2" y="7" rx="2"/><path d="M22 11v2"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "battery-charging": '<path d="M15 7h1a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-2M6 17H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h2.5"/><path d="m11 7-3 5h4l-3 5M22 11v2"/>',
    "map-pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/>',
    "qr": '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/><path d="M14 14h3v3M21 14v.01M14 21h7v-4"/>',
    "home": '<path d="M3 11 12 3l9 8"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "switch": '<path d="M7 4 3 8l4 4"/><path d="M3 8h14"/><path d="m17 20 4-4-4-4"/><path d="M21 16H7"/>',
}


@register.simple_tag
def icon(name, size=18):
    return format_html(
        '<svg class="icon" width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" focusable="false">{p}</svg>',
        s=size,
        p=mark_safe(ICONS.get(name, "")),
    )


@register.simple_tag
def health_badge(health):
    return _badge(HEALTH_BADGES, health)


@register.simple_tag
def device_badge(state):
    return _badge(DEVICE_BADGES, state)


@register.filter
def cron_label(expr) -> str:
    from apps.operations.cron import describe

    return describe(expr)


@register.filter
def avatar_tone(value) -> int:
    """A stable colour (0-5) per person, so each child's avatar is recognisable."""
    import hashlib

    return int(hashlib.md5(str(value).encode(), usedforsecurity=False).hexdigest(), 16) % 6


@register.filter
def initials(name) -> str:
    """'Sara Rahman' -> 'SR' (same rule as Person.initials, for a plain name)."""
    parts = str(name).split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


@register.filter
def map_link(obj) -> str:
    """OpenStreetMap at this device's (or location row's) position."""
    from apps.devices.maps import open_url

    return open_url(obj.latitude, obj.longitude)


@register.filter
def device_icon(device_or_platform):
    platform = getattr(device_or_platform, "platform", device_or_platform)
    return "phone" if platform in ("android", "ios") else "laptop"


@register.filter
def status_counts(rows) -> dict:
    """{status: how many} for a list of dashboard rows (each has .status)."""
    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


@register.filter
def health_tone(health):
    return HEALTH_BADGES.get(health, ("muted",))[0]


@register.simple_tag
def command_badge(status):
    return _badge(COMMAND_BADGES, status)


@register.simple_tag
def monitor_badge(state):
    return _badge(MONITOR_BADGES, state)


@register.simple_tag
def service_badge(state):
    return _badge(SERVICE_BADGES, state)


def _tone(value, warn=70, crit=90):
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "good"


@register.simple_tag
def meter(value, label=""):
    """A percentage bar. The number is always printed, so the colour is never the only cue."""
    if value is None:
        return mark_safe('<span class="empty-val">—</span>')
    v = max(0.0, min(100.0, float(value)))
    return format_html(
        '<div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" '
        'aria-valuenow="{v}" aria-label="{label}">'
        '<div class="meter-track"><div class="meter-fill meter-{tone}" style="width:{w}%">'
        '</div></div><span class="meter-value">{v}%</span></div>',
        v=f"{v:.0f}",
        w=f"{v:.1f}",
        tone=_tone(v),
        label=label,
    )


@register.simple_tag
def ring(value, label=""):
    """Donut gauge for a percentage; the number sits in the middle so colour is never the only cue."""
    if value is None:
        return format_html(
            '<div class="ring ring-empty" role="img" aria-label="{} unknown">'
            '<svg viewBox="0 0 36 36"><circle class="ring-track" cx="18" cy="18" r="15.5"/></svg>'
            '<span class="ring-value">—</span></div>',
            label,
        )
    v = max(0.0, min(100.0, float(value)))
    return format_html(
        '<div class="ring ring-{tone}" role="meter" aria-valuemin="0" aria-valuemax="100" '
        'aria-valuenow="{v}" aria-label="{label}"><svg viewBox="0 0 36 36">'
        '<circle class="ring-track" cx="18" cy="18" r="15.5"/>'
        '<circle class="ring-fill" cx="18" cy="18" r="15.5" pathLength="100" '
        'stroke-dasharray="{w} 100"/></svg><span class="ring-value">{v}<small>%</small></span></div>',
        v=f"{v:.0f}",
        w=f"{v:.1f}",
        tone=_tone(v),
        label=label,
    )


@register.filter
def sub(value, other):
    """a|sub:b -> a - b (e.g. a disk's free bytes from total and used); "" on bad input."""
    try:
        return max(0, value - other)
    except TypeError:
        return ""


@register.filter
def pct_tone(value):
    """good / warning / critical for a percentage, the same thresholds as meters and rings."""
    return "muted" if value is None else _tone(float(value))


@register.simple_tag
def trendline(points, start, end):
    """A bare line for a metric tile: no axes, no labels, just the shape of the last hour.
    Decorative (the tile prints the number), so it's hidden from assistive tech."""
    if len(points or ()) < 2:
        return mark_safe('<div class="trend trend-empty" aria-hidden="true"></div>')
    span = max(1, end - start)
    pts = " ".join(
        f"{(t - start) / span * TREND_W:.1f},{TREND_H - min(v, 100) / 100 * TREND_H:.1f}"
        for t, v in points
    )
    # format_html escapes its arguments to strings first, so numbers are formatted here.
    first_x = f"{(points[0][0] - start) / span * TREND_W:.1f}"
    last_x = f"{(points[-1][0] - start) / span * TREND_W:.1f}"
    return format_html(
        '<svg class="trend" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
        '<polygon class="trend-area" points="{first},{h} {pts} {last},{h}"/>'
        '<polyline class="trend-line" points="{pts}" vector-effect="non-scaling-stroke"/></svg>',
        w=TREND_W,
        h=TREND_H,
        pts=pts,
        first=first_x,
        last=last_x,
    )


TREND_W, TREND_H = 200, 40


@register.filter
def count_state(items, state):
    """How many service dicts are in `state` (e.g. "up")."""
    return sum(1 for i in items or () if i.get("state") == state)


@register.filter
def duration(seconds):
    if seconds in (None, ""):
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@register.filter
def ago(value):
    """Compact relative time with one unit ("3 hours ago"), for dense tables."""
    if not value:
        return "never"
    if (timezone.now() - value).total_seconds() < 60:
        return "just now"
    return f"{timesince(value, depth=1)} ago"


@register.filter
def until(value):
    """Compact time to come with one unit ("in 7 hours"); "now" once it's due."""
    if not value:
        return ""
    from django.utils.timesince import timeuntil

    if (value - timezone.now()).total_seconds() < 60:
        return "now"
    return f"in {timeuntil(value, depth=1)}"


@register.filter
def duration_td(delta):
    """timedelta -> "3h 12m" / "45s"."""
    if delta is None:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    return duration(seconds)


@register.filter
def every(seconds):
    """Interval in seconds -> "30s" / "5m" / "1h 30m"."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds < 3600:
        return f"{seconds // 60}m" + (f" {seconds % 60}s" if seconds % 60 else "")
    return duration(seconds)


@register.filter
def bps(value):
    if value in (None, ""):
        return "—"
    value = float(value)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if hasattr(mapping, "get") else None


SPARK_W, SPARK_H = 600, 120


def _fmt(value: float, unit: str) -> str:
    return f"{value:.0f}{unit}" if unit == "%" else f"{value:,.0f} {unit}"


@register.simple_tag
def sparkline(points, start, end, label, ymax=100, unit="%"):
    """Line chart of values in [0, ymax] over [start, end]. Gaps longer than three buckets
    break the line, so an outage shows as a hole instead of a misleading straight segment."""
    ymax = max(1, float(ymax))
    span = max(1, end - start)
    gap = span / 120 * 3
    segments, current, prev_t = [], [], None
    for t, v in points:
        if prev_t is not None and t - prev_t > gap:
            segments.append(current)
            current = []
        x = (t - start) / span * SPARK_W
        y = SPARK_H - (min(v, ymax) / ymax) * SPARK_H
        current.append(f"{x:.1f},{y:.1f}")
        prev_t = t
    if current:
        segments.append(current)
    grad_id = "spark-fill-" + slugify(label)
    areas = format_html_join(
        "",
        '<polygon class="spark-area" fill="url(#{})" points="{} {},{} {},{}"/>',
        (
            (grad_id, " ".join(seg), seg[-1].split(",")[0], SPARK_H, seg[0].split(",")[0], SPARK_H)
            for seg in segments
            if len(seg) > 1
        ),
    )
    lines = format_html_join(
        "",
        '<polyline class="spark-line" points="{}" vector-effect="non-scaling-stroke"/>',
        ((" ".join(seg),) for seg in segments if seg),
    )
    grid = format_html_join(
        "",
        '<line class="spark-grid" x1="0" x2="{}" y1="{}" y2="{}" '
        'vector-effect="non-scaling-stroke"/>',
        ((SPARK_W, SPARK_H * f, SPARK_H * f) for f in (0, 0.5)),
    )
    values = [v for _, v in points]
    stats = (
        format_html(
            '<dl class="spark-stats"><div><dt>Now</dt><dd>{}</dd></div>'
            "<div><dt>Avg</dt><dd>{}</dd></div><div><dt>Max</dt><dd>{}</dd></div></dl>",
            _fmt(values[-1], unit),
            _fmt(sum(values) / len(values), unit),
            _fmt(max(values), unit),
        )
        if values
        else mark_safe('<p class="text-muted small mb-0">No data in this range.</p>')
    )
    return format_html(
        '<figure class="spark" data-points="{data}" data-start="{start}" data-end="{end}" '
        'data-unit="{unit}">'
        '<figcaption><span class="spark-title">{label}</span>{stats}</figcaption>'
        '<div class="spark-plot"><svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
        'role="img" aria-label="{label} over time">'
        '<defs><linearGradient id="{grad_id}" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" class="spark-stop-top"/><stop offset="1" class="spark-stop-bottom"/>'
        "</linearGradient></defs>{grid}"
        '<line class="spark-base" x1="0" x2="{w}" y1="{h}" y2="{h}" vector-effect="non-scaling-stroke"/>'
        "{areas}{lines}</svg>"
        '<span class="spark-axis spark-axis-top">{top}</span>'
        '<span class="spark-axis spark-axis-mid">{mid}</span>'
        '<div class="spark-cursor" hidden></div><div class="spark-tip" hidden></div></div>'
        "</figure>",
        data=json.dumps(points),
        start=start,
        end=end,
        label=label,
        stats=stats,
        w=SPARK_W,
        h=SPARK_H,
        grid=grid,
        areas=areas,
        grad_id=grad_id,
        lines=lines,
        unit=unit,
        top=_fmt(ymax, unit),
        mid=_fmt(ymax / 2, unit),
    )


@register.filter
def fills_in(full_at):
    """Server.disk_full_at -> "fills in ~6 days" (only within 30 days; otherwise "")."""
    if not full_at:
        return ""
    seconds = (full_at - timezone.now()).total_seconds()
    if seconds > 30 * 86400:
        return ""
    if seconds < 86400:
        return f"fills in ~{max(int(seconds // 3600), 0)} h"
    days = round(seconds / 86400)
    return f"fills in ~{days} day{pluralize(days)}"


KIND_ICONS = {"http": "globe", "tcp": "plug", "ping": "activity", "dns": "layers",
              "domain": "globe", "heartbeat": "clock"}  # fmt: skip


@register.filter
def kind_icon(kind):
    return KIND_ICONS.get(kind, "globe")


@register.filter
def tone_of(status):
    return {"up": "good", "degraded": "warning", "down": "critical"}.get(status, "muted")
