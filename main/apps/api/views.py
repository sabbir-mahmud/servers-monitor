"""REST API v1 (documented in docs/api.md) and the Prometheus export.

Plain JSON views, no framework: the surface is small, read-mostly and fixed. Writes go
through the same services as the UI (validation, allowlists, audit), so the API can do
nothing a person with the same role couldn't do in the browser. Free-form shell is
browser-only.
"""

import uuid

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.alerts.models import Alert
from apps.fleet.models import Health, Server
from apps.monitors import stats
from apps.monitors.models import Monitor, MonitorStatus
from apps.operations import services as ops
from apps.operations.models import Command, Job

from .auth import api_view, error

MAX_LIMIT = 500


def _ts(value):
    return value.isoformat() if value else None


def _page(request, qs, serialize):
    try:
        limit = max(1, min(MAX_LIMIT, int(request.GET.get("limit", 100))))
        offset = max(0, int(request.GET.get("offset", 0)))
    except ValueError:
        return error(400, "limit and offset must be numbers.")
    rows = list(qs[offset : offset + limit + 1])
    more = len(rows) > limit
    return JsonResponse(
        {
            "results": [serialize(r) for r in rows[:limit]],
            "next_offset": offset + limit if more else None,
        }  # fmt: skip
    )


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------- servers


def server_json(s: Server, detail: bool = False) -> dict:
    out = {
        "id": str(s.pk),
        "name": s.name,
        "environment": s.environment,
        "groups": [g.name for g in s.groups.all()],
        "enabled": s.is_enabled,
        "health": s.health,
        "last_seen_at": _ts(s.last_seen_at),
        "agent_version": s.agent_version,
        "hostname": s.hostname,
        "metrics": {
            "cpu_percent": s.cpu_percent,
            "memory_percent": s.memory_percent,
            "disk_percent": s.disk_percent,
            "load_1": s.load_1,
            "net_rx_bps": s.net_rx_bps,
            "net_tx_bps": s.net_tx_bps,
        },
        "disk_full_at": _ts(s.disk_full_at),
    }
    if detail:
        out.update(
            description=s.description,
            platform=s.platform,
            cpu_count=s.cpu_count,
            memory_total=s.memory_total,
            uptime_seconds=s.uptime_seconds,
            services=s.services,
            disks=s.disks,
            actions=s.actions,
            logs=s.log_names,
            features=s.capabilities.get("features", []),
        )
    return out


@api_view(perm="fleet.view_server")
def servers(request):
    qs = Server.objects.prefetch_related("groups").order_by("name")
    if env := request.GET.get("environment"):
        qs = qs.filter(environment=env)
    if group := request.GET.get("group"):
        qs = qs.filter(groups__name=group)
    return _page(request, qs, server_json)


@api_view(perm="fleet.view_server")
def server_detail(request, pk):
    server = get_object_or_404(Server.objects.prefetch_related("groups"), pk=pk)
    return JsonResponse(server_json(server, detail=True))


# --------------------------------------------------------------------------- monitors


def monitor_json(m: Monitor, uptime: dict | None = None) -> dict:
    return {
        "id": str(m.pk),
        "name": m.name,
        "kind": m.kind,
        "target": m.where,
        "enabled": m.is_enabled,
        "status": m.display_status,
        "status_since": _ts(m.status_since),
        "last_checked_at": _ts(m.last_checked_at),
        "last_response_ms": m.last_response_ms,
        "last_status_code": m.last_status_code,
        "last_error": m.last_error,
        "uptime_24h": (uptime or {}).get(m.pk),
        "tls_expires_at": _ts(m.tls_expires_at),
        "domain_expires_at": _ts(m.domain_expires_at),
        "server": str(m.server_id) if m.server_id else None,
    }


@api_view(perm="monitors.view_monitor")
def monitors(request):
    qs = Monitor.objects.order_by("name")
    if status := request.GET.get("status"):
        qs = qs.filter(status=status, is_enabled=True)
    uptime = stats.uptime_by_monitor(list(qs.values_list("pk", flat=True)), 24)
    return _page(request, qs, lambda m: monitor_json(m, uptime))


@api_view(perm="monitors.view_monitor")
def monitor_detail(request, pk):
    m = get_object_or_404(Monitor, pk=pk)
    return JsonResponse(monitor_json(m, stats.uptime_by_monitor([m.pk], 24)))


# --------------------------------------------------------------------------- alerts


def alert_json(a: Alert) -> dict:
    return {
        "id": str(a.pk),
        "kind": a.kind,
        "severity": a.severity,
        "message": a.message,
        "server": str(a.server_id) if a.server_id else None,
        "monitor": str(a.monitor_id) if a.monitor_id else None,
        "opened_at": _ts(a.opened_at),
        "resolved_at": _ts(a.resolved_at),
        "acknowledged_at": _ts(a.acknowledged_at),
    }


@api_view(perm="alerts.view_alert")
def alerts(request):
    qs = Alert.objects.order_by("-opened_at")
    state = request.GET.get("state", "open")
    if state == "open":
        qs = qs.filter(resolved_at__isnull=True)
    elif state == "resolved":
        qs = qs.filter(resolved_at__isnull=False)
    return _page(request, qs, alert_json)


# --------------------------------------------------------------------------- jobs


def command_json(c: Command, output: bool = False) -> dict:
    out = {
        "id": str(c.pk),
        "server": str(c.server_id),
        "server_name": c.server.name,
        "status": c.status,
        "exit_code": c.exit_code,
        "batch": c.batch,
        "created_at": _ts(c.created_at),
        "delivered_at": _ts(c.delivered_at),
        "finished_at": _ts(c.finished_at),
    }
    if output:
        out.update(output=c.output, output_truncated=c.output_truncated)
    return out


def job_json(j: Job, detail: bool = False) -> dict:
    out = {
        "id": str(j.pk),
        "action": j.action,
        "summary": j.summary,
        "note": j.note,
        "created_by": j.created_by.get_username() if j.created_by else None,
        "created_at": _ts(j.created_at),
        "strategy": j.strategy,
        "state": j.state,
        "counts": {k: v for k, v in ops.job_counts(j).items() if v},
    }
    if detail:
        out["args"] = j.args
        out["commands"] = [command_json(c) for c in j.commands.select_related("server")]
    return out


def _create_job(request):
    body = request.json
    action = str(body.get("action") or "")
    if not action:
        return error(400, "Name the action to run.")
    if action == "shell" or action.startswith("_"):
        return error(403, "This action is only available in the dashboard.")
    wanted = body.get("servers")
    if not isinstance(wanted, list) or not wanted:
        return error(400, "servers must be a non-empty list of server ids or names.")
    ids = [u for u in (_uuid(w) for w in wanted) if u]
    names = [str(w) for w in wanted if not _uuid(w)]
    targets = list(Server.objects.filter(Q(pk__in=ids) | Q(name__in=names)))
    if len(targets) != len(set(map(str, wanted))):
        found = {str(s.pk) for s in targets} | {s.name for s in targets}
        return error(
            400, f"Unknown servers: {', '.join(str(w) for w in wanted if str(w) not in found)}"
        )
    rollout = body.get("rollout")
    if rollout is not None:
        if not isinstance(rollout, dict):
            return error(400, "rollout must be an object.")
        try:
            rollout = {"batch_size": max(1, min(500, int(rollout.get("batch_size", 1)))),
                       "pause_seconds": max(0, min(3600, int(rollout.get("pause_seconds", 0)))),
                       "stop_on_failure": bool(rollout.get("stop_on_failure", True))}  # fmt: skip
        except TypeError, ValueError:
            return error(400, "rollout values must be numbers.")
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return error(400, "args must be an object.")
    try:
        job, skipped = ops.create_job(request, action, args, targets,
                                      note=str(body.get("note") or "")[:200], rollout=rollout)  # fmt: skip
    except ops.JobError as exc:
        return error(400, str(exc))
    out = job_json(job, detail=True)
    out["skipped"] = {s.name: reason for s, reason in skipped}
    return JsonResponse(out, status=201)


@api_view(perm="operations.view_job", methods=("GET", "POST"), write_perm="operations.add_job")
def jobs(request):
    if request.method == "POST":
        return _create_job(request)
    return _page(
        request, Job.objects.select_related("created_by").prefetch_related("commands"), job_json
    )


@api_view(perm="operations.view_job")
def job_detail(request, pk):
    return JsonResponse(job_json(get_object_or_404(Job, pk=pk), detail=True))


@api_view(perm="operations.view_command")
def command_detail(request, pk):
    command = get_object_or_404(Command.objects.select_related("server"), pk=pk)
    return JsonResponse(command_json(command, output=True))


# --------------------------------------------------------------------------- prometheus


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Exposition:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, help_text: str, kind: str, samples) -> None:
        samples = [(labels, value) for labels, value in samples if value is not None]
        self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            inner = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            self.lines.append(
                f"{name}{{{inner}}} {float(value):g}" if inner else f"{name} {float(value):g}"
            )

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@api_view(perm="fleet.view_server")
def metrics(request):
    """Prometheus text format. Scrape with `authorization: {credentials: smt_…}`."""
    now = timezone.now()
    out = _Exposition()
    servers = list(Server.objects.filter(is_enabled=True).order_by("name"))
    base = [(s, {"server": s.name, "environment": s.environment}) for s in servers]
    out.metric(
        "sm_build_info", "Dashboard version.", "gauge", [({"version": settings.APP_VERSION}, 1)]
    )
    out.metric("sm_server_up", "1 if the agent reported recently.", "gauge",
               [(lb, s.health_at(now) == Health.ONLINE) for s, lb in base])  # fmt: skip
    out.metric("sm_server_last_seen_timestamp_seconds", "Last heartbeat.", "gauge",
               [(lb, s.last_seen_at.timestamp() if s.last_seen_at else None) for s, lb in base])  # fmt: skip
    for field, help_text in (("cpu_percent", "CPU use."), ("memory_percent", "Memory use."),
                             ("disk_percent", "Fullest disk."), ("load_1", "1-minute load."),
                             ("net_rx_bps", "Bytes received per second."),
                             ("net_tx_bps", "Bytes sent per second.")):  # fmt: skip
        out.metric(
            f"sm_server_{field}", help_text, "gauge", [(lb, getattr(s, field)) for s, lb in base]
        )
    out.metric("sm_server_disk_used_percent", "Use per mount.", "gauge",
               [({**lb, "mount": d.get("mount")}, d.get("percent")) for s, lb in base for d in s.disks])  # fmt: skip
    out.metric("sm_service_up", "1 if the service is up.", "gauge",
               [({**lb, "service": v.get("name"), "kind": v.get("kind")}, v.get("state") == "up")
                for s, lb in base for v in s.services])  # fmt: skip

    mons = list(Monitor.objects.filter(is_enabled=True).order_by("name"))
    mlb = [(m, {"monitor": m.name, "kind": m.kind}) for m in mons]
    out.metric("sm_monitor_up", "1 if up or slow, 0 if down.", "gauge",
               [(lb, m.status != MonitorStatus.DOWN) for m, lb in mlb if m.status != MonitorStatus.PENDING])  # fmt: skip
    out.metric("sm_monitor_response_ms", "Last response time.", "gauge",
               [(lb, m.last_response_ms) for m, lb in mlb])  # fmt: skip
    out.metric("sm_monitor_tls_expiry_timestamp_seconds", "Certificate expiry.", "gauge",
               [(lb, m.tls_expires_at.timestamp() if m.tls_expires_at else None) for m, lb in mlb])  # fmt: skip
    counts = {}
    for sev in Alert.objects.filter(resolved_at__isnull=True).values_list("severity", flat=True):
        counts[sev] = counts.get(sev, 0) + 1
    out.metric("sm_alerts_open", "Open alerts.", "gauge",
               [({"severity": sev}, counts.get(sev, 0)) for sev in ("warning", "critical")])  # fmt: skip
    return HttpResponse(out.text(), content_type="text/plain; version=0.0.4; charset=utf-8")
