"""Server lifecycle and heartbeat ingestion. Views and the agent API call these; they hold
the multi-model writes so every path gets the same transaction and audit behaviour."""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.alerts import services as alert_services
from apps.audit.services import record

from .models import Health, MetricSample, Server
from .tokens import hash_token


def _relay():
    from apps.agent_api import relay  # late: avoids an import cycle via the bridge

    return relay


def _remember_credential(server: Server, agent_url: str) -> None:
    """Store only a digest and a short display prefix of the agent's relay password."""
    from urllib.parse import unquote, urlsplit

    password = unquote(urlsplit(agent_url).password or "")
    server.token_hash, server.token_prefix = hash_token(password), password[:8]


logger = logging.getLogger(__name__)


@transaction.atomic
def create_server(request, form) -> tuple[Server, str]:
    """Save a new server and create its relay (Redis ACL) user. Returns (server, the agent's
    connection URL); the URL holds the password and is shown once, never stored."""
    server = form.save(commit=False)
    server.created_by = request.user
    server.save()  # the pk names the ACL user and its keys
    agent_url = _relay().provision(server)  # RelayError rolls the whole create back
    _remember_credential(server, agent_url)
    server.save(update_fields=["token_hash", "token_prefix"])
    form.save_m2m()
    record(request, "server.create", target=server.name, server=server)
    return server, agent_url


@transaction.atomic
def rotate_token(request, server: Server) -> str:
    """Issue a new relay password; the old one stops working immediately."""
    agent_url = _relay().provision(server)
    _remember_credential(server, agent_url)
    server.save(update_fields=["token_hash", "token_prefix", "updated_at"])
    record(request, "server.rotate_token", target=server.name, server=server)
    return agent_url


def server_settings_changed(server: Server, changed: list[str]) -> None:
    """Push dashboard-side changes the agent must know about right away."""
    relay = _relay()
    try:
        if "is_enabled" in changed:
            relay.set_enabled(server)
        if "report_interval" in changed and server.is_enabled:
            relay.push(server, {"t": "cfg", "report_interval": server.report_interval})
    except Exception as exc:  # the next heartbeat re-syncs the interval anyway
        logger.warning("Could not notify %s's agent of new settings: %s", server.name, exc)


def delete_server(server: Server) -> None:
    _relay().deprovision(server)
    server.delete()


def ingest_report(server: Server, report: dict, remote_addr: str | None = None) -> None:
    """Store one validated heartbeat: snapshot on the server row, a sample, alert rules."""
    now = timezone.now()
    with transaction.atomic():
        for field in (
            "hostname",
            "platform",
            "agent_version",
            "cpu_count",
            "memory_total",
            "uptime_seconds",
            "capabilities",
            "services",
            "disks",
            "cpu_percent",
            "memory_percent",
            "disk_percent",
            "load_1",
            "net_rx_bps",
            "net_tx_bps",
        ):
            setattr(server, field, report[field])
        server.remote_addr = remote_addr
        server.last_seen_at = now
        was = server.last_health
        server.last_health = Health.ONLINE
        server.save(
            update_fields=[
                "hostname",
                "platform",
                "agent_version",
                "cpu_count",
                "memory_total",
                "uptime_seconds",
                "capabilities",
                "services",
                "disks",
                "cpu_percent",
                "memory_percent",
                "disk_percent",
                "load_1",
                "net_rx_bps",
                "net_tx_bps",
                "remote_addr",
                "last_seen_at",
                "last_health",
            ]
        )
        MetricSample.objects.create(
            server=server,
            recorded_at=now,
            cpu_percent=report["cpu_percent"],
            memory_percent=report["memory_percent"],
            disk_percent=report["disk_percent"],
            load_1=report["load_1"],
            net_rx_bps=report["net_rx_bps"],
            net_tx_bps=report["net_tx_bps"],
        )
        if server.is_enabled:
            alert_services.evaluate_report(server, back_online=was == Health.OFFLINE)


def prune_samples(batch_size: int = 5000) -> int:
    """Delete samples past retention in bounded batches so no single DELETE holds locks long."""
    cutoff = MetricSample.retention_cutoff()
    total = 0
    while True:
        ids = list(
            MetricSample.objects.filter(recorded_at__lt=cutoff).values_list("pk", flat=True)[
                :batch_size
            ]
        )
        if not ids:
            return total
        total += MetricSample.objects.filter(pk__in=ids).delete()[0]


CHART_RANGES = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
CHART_POINTS = 120


def chart_series(server: Server, range_key: str) -> dict:
    """Bucket-averaged CPU/memory/disk for the range, at most CHART_POINTS per series.

    Averaged in Python over a values_list: 7 days at 30 s is ~20k narrow rows, which is a
    single index range scan. Move to Postgres `date_bin` or a rollup table past that.
    """
    span = CHART_RANGES.get(range_key, CHART_RANGES["1h"])
    end = timezone.now()
    start = end - span
    bucket = span.total_seconds() / CHART_POINTS
    rows = (
        MetricSample.objects.filter(server=server, recorded_at__gte=start)
        .order_by("recorded_at")
        .values_list("recorded_at", "cpu_percent", "memory_percent", "disk_percent")
    )
    buckets: dict[int, list] = {}
    for ts, cpu, mem, disk in rows:
        idx = min(int((ts - start).total_seconds() // bucket), CHART_POINTS - 1)
        acc = buckets.setdefault(idx, [ts, 0.0, 0.0, 0.0, 0])
        acc[0] = ts
        acc[1] += cpu
        acc[2] += mem
        acc[3] += disk
        acc[4] += 1
    series = {"cpu": [], "memory": [], "disk": []}
    for idx in sorted(buckets):
        ts, cpu, mem, disk, n = buckets[idx]
        epoch = int(ts.timestamp())
        series["cpu"].append([epoch, round(cpu / n, 1)])
        series["memory"].append([epoch, round(mem / n, 1)])
        series["disk"].append([epoch, round(disk / n, 1)])
    return {"start": int(start.timestamp()), "end": int(end.timestamp()), **series}


def fleet_summary(servers) -> dict:
    now = timezone.now()
    counts = {h.value: 0 for h in Health}
    for s in servers:
        counts[s.health_at(now)] += 1
    counts["total"] = len(servers)
    return counts


def log_settings_change(request, server: Server, changed: list[str]) -> None:
    if changed:
        record(request, "server.update", target=server.name, server=server, fields=changed)
        logger.info("Server %s updated: %s", server.name, ", ".join(changed))
