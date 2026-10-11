"""Alert rules. Threshold and service rules run inside the heartbeat transaction; offline
detection runs in the periodic sweep (a dead server sends no heartbeat to evaluate)."""

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.fleet.models import Health, MetricSample, Server

from .models import Alert, AlertKind, Severity

logger = logging.getLogger(__name__)

CRITICAL_PERCENT = 97.0
DOWN_STATES = {"down"}


def _open(server, open_alerts, kind, message, severity, subject="", value=None) -> None:
    existing = open_alerts.get((kind, subject))
    if existing is not None:
        if existing.value != value or existing.severity != severity:
            existing.value, existing.severity, existing.message = value, severity, message
            existing.save(update_fields=["value", "severity", "message"])
        return
    try:
        with transaction.atomic():  # savepoint: a lost race must not poison the heartbeat
            alert = Alert.objects.create(
                server=server,
                kind=kind,
                subject=subject,
                severity=severity,
                message=message[:300],
                value=value,
            )
    except IntegrityError:
        logger.info("Alert %s/%s on %s already opened concurrently", kind, subject, server)
        return
    open_alerts[(kind, subject)] = alert  # the relay's notification outbox announces it


def _resolve(open_alerts, kind, subject="") -> None:
    alert = open_alerts.pop((kind, subject), None)
    if alert is None:
        return
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["resolved_at"])


def _threshold(server, open_alerts, kind, value, threshold, label) -> None:
    if value is None:
        return
    if value >= threshold:
        severity = Severity.CRITICAL if value >= CRITICAL_PERCENT else Severity.WARNING
        _open(
            server,
            open_alerts,
            kind,
            f"{label} at {value:.0f}% on {server.name}",
            severity,
            value=value,
        )
    elif value < threshold - settings.ALERT_HYSTERESIS_PERCENT:
        _resolve(open_alerts, kind)


def _open_alerts(server) -> dict:
    return {(a.kind, a.subject): a for a in Alert.objects.filter(server=server, resolved_at=None)}


def evaluate_report(server: Server, back_online: bool = False) -> None:
    open_alerts = _open_alerts(server)
    if back_online:
        _resolve(open_alerts, AlertKind.OFFLINE)

    recent_cpu = list(
        MetricSample.objects.filter(server=server)
        .order_by("-recorded_at")
        .values_list("cpu_percent", flat=True)[: settings.ALERT_CPU_SAMPLES]
    )
    # Not enough history yet means no CPU verdict either way: one spike is not an incident.
    cpu_avg = (
        sum(recent_cpu) / len(recent_cpu) if len(recent_cpu) >= settings.ALERT_CPU_SAMPLES else None
    )
    _threshold(server, open_alerts, AlertKind.CPU, cpu_avg, settings.ALERT_CPU_PERCENT, "CPU")
    _threshold(
        server,
        open_alerts,
        AlertKind.MEMORY,
        server.memory_percent,
        settings.ALERT_MEMORY_PERCENT,
        "Memory",
    )
    _threshold(
        server,
        open_alerts,
        AlertKind.DISK,
        server.disk_percent,
        settings.ALERT_DISK_PERCENT,
        "Disk",
    )

    reported = set()
    for svc in server.services:
        name = svc["name"]
        reported.add(name)
        if svc.get("state") in DOWN_STATES:
            detail = svc.get("status") or "down"
            _open(
                server,
                open_alerts,
                AlertKind.SERVICE,
                f"{name} is {detail} on {server.name}",
                Severity.CRITICAL,
                subject=name,
            )
        elif svc.get("state") == "up":
            _resolve(open_alerts, AlertKind.SERVICE, name)
    # A service removed from the agent config can no longer recover; close its alert.
    for kind, subject in list(open_alerts):
        if kind == AlertKind.SERVICE and subject not in reported:
            _resolve(open_alerts, kind, subject)


def resolve_all(server: Server) -> None:
    """Close every open alert (server disabled or deleted from monitoring)."""
    open_alerts = _open_alerts(server)
    for kind, subject in list(open_alerts):
        _resolve(open_alerts, kind, subject)


def sweep(now=None) -> dict:
    """Mark silent servers late/offline and open offline alerts on the transition."""
    from apps.operations.services import expire_commands

    now = now or timezone.now()
    went_offline = 0
    servers = Server.objects.filter(is_enabled=True, last_seen_at__isnull=False).only(
        "id", "name", "is_enabled", "report_interval", "last_seen_at", "last_health"
    )
    for server in servers:
        health = server.health_at(now)
        if health == server.last_health or health == Health.ONLINE:
            continue
        with transaction.atomic():
            # Conditional update: if a heartbeat landed since we read the row, it wins.
            changed = Server.objects.filter(
                pk=server.pk, last_health=server.last_health, last_seen_at=server.last_seen_at
            ).update(last_health=health)
            if changed and health == Health.OFFLINE:
                went_offline += 1
                silent = int((now - server.last_seen_at).total_seconds())
                ago = f"{silent} s" if silent < 120 else f"{silent // 60} min"
                _open(
                    server,
                    _open_alerts(server),
                    AlertKind.OFFLINE,
                    f"{server.name} has not reported for {ago}",
                    Severity.CRITICAL,
                )
    expired = expire_commands(now)
    return {"offline": went_offline, "expired_commands": expired}
