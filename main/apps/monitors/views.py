from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.alerts import maintenance
from apps.alerts.models import Alert
from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin
from apps.core.utils import safe_next
from apps.fleet.views import MUTE_CHOICES

from . import checker, stats
from .forms import MonitorForm
from .models import Monitor, MonitorStatus


class MonitorListView(StaffViewMixin, HtmxTemplateMixin, ListView):
    """Every application with its 24 h uptime bar. The `board` partial refreshes every 15 s."""

    permission_required = "monitors.view_monitor"
    template_name = "monitors/list.html"
    htmx_partial = "board"
    context_object_name = "monitors"

    def get_queryset(self):
        return Monitor.objects.select_related("server")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        monitors = list(ctx["monitors"])
        ids = [m.pk for m in monitors]
        uptime = stats.uptime_by_monitor(ids, 24)
        avg = stats.avg_response_by_monitor(ids, 24)
        bars = stats.timelines(ids, hours=24, buckets=48)
        for m in monitors:
            m.uptime_24h = uptime.get(m.pk)
            m.avg_ms = avg.get(m.pk)
            m.bar = bars.get(m.pk, [])
        states = [m.display_status for m in monitors]
        ctx.update(
            monitors=monitors,
            summary={
                "total": len(monitors),
                "up": states.count(MonitorStatus.UP),
                "down": states.count(MonitorStatus.DOWN),
                "paused": states.count(MonitorStatus.PAUSED),
            },
            overall_uptime=(sum(uptime.values()) / len(uptime)) if uptime else None,
        )
        return ctx


class MonitorDetailView(StaffViewMixin, DetailView):
    permission_required = "monitors.view_monitor"
    template_name = "monitors/detail.html"
    model = Monitor
    context_object_name = "monitor"

    def get_template_names(self):
        part = self.request.GET.get("part")
        if self.request.htmx and part in ("live", "chart"):
            return [f"{self.template_name}#{part}"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        range_key = self.request.GET.get("range", "24h")
        if range_key not in stats.RANGES:
            range_key = "24h"
        ids = [m.pk]
        locations = _locations(m)
        recent = m.checks.all()
        if locations:
            recent = recent.exclude(location="")  # each location's own results
        recent = list(recent[:15])
        names = dict(
            zip([r["key"] for r in locations], [r["name"] for r in locations], strict=True)
        )
        for c in recent:
            c.location_name = names.get(c.location, "removed server") if c.location else ""
        ctx.update(
            locations=locations,
            uptime={
                label: stats.uptime_by_monitor(ids, h).get(m.pk)
                for label, h in (("24h", 24), ("7d", 168), ("14d", 336))
            },  # fmt: skip
            avg_ms=stats.avg_response_by_monitor(ids, 24).get(m.pk),
            bar=stats.timelines(ids, hours=24, buckets=72)[m.pk],
            range_key=range_key,
            ranges=list(stats.RANGES),
            series=stats.response_series(m, range_key),
            incidents=stats.incidents(m),
            recent=recent,
            open_alerts=Alert.objects.filter(monitor=m, resolved_at__isnull=True),
            can_change=self.request.user.has_perm("monitors.change_monitor"),
            maintenance_window=maintenance.coverage().window_for_monitor(m.pk, m.server_id),
            mute_choices=MUTE_CHOICES,
        )
        return ctx


def _locations(monitor) -> list[dict]:
    """Each location's latest result for a monitor checked from several places, or []."""
    from datetime import datetime

    if not monitor.supports_locations:
        return []
    servers = list(
        monitor.probe_servers.only(
            "name", "capabilities", "last_seen_at", "report_interval", "is_enabled"
        )
    )
    if not servers:
        return []
    now = timezone.now()
    states = monitor.location_states or {}
    rows = []
    entries = (
        [(checker.DASHBOARD, "This dashboard", None)] if monitor.check_from_dashboard else []
    ) + [(str(s.pk), s.name, s) for s in servers]
    for key, name, server in entries:
        st = states.get(key)
        at = datetime.fromisoformat(st["at"]) if st else None
        stale = at is None or (now - at).total_seconds() > checker._stale_after(monitor)
        note = ""
        if server is not None and "probes" not in (server.capabilities.get("features") or []):
            note = "probes are off on this server (sudo smctl probes on)"
        rows.append({"key": key, "name": name, "server": server, "state": st, "at": at,
                     "stale": stale, "detail": note or (st or {}).get("error", ""),
                     "down": bool(st) and not st["ok"] and st["failures"] >= monitor.failure_threshold})  # fmt: skip
    return rows


FORM_SECTIONS = [
    ("What to check", ["kind", "name", "url", "method", "target", "port", "tls", "record_type",
                       "expected", "resolver", "period_seconds", "grace_seconds"]),
    ("Schedule", ["interval_seconds", "timeout_seconds", "failure_threshold"]),
    ("What counts as up", ["expected_status", "keyword", "keyword_absent", "slow_ms",
                           "verify_tls", "follow_redirects"]),
    ("Request", ["body", "headers", "clear_headers"]),
    ("Where from", ["probe_servers", "agents_only", "quorum"]),
    ("Organise", ["server", "is_enabled"]),
]  # fmt: skip


class MonitorCreateView(StaffViewMixin, CreateView):
    permission_required = "monitors.add_monitor"
    template_name = "monitors/form.html"
    form_class = MonitorForm
    extra_context = {"sections": FORM_SECTIONS}

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.next_check_at = timezone.now()  # first check right away
        response = super().form_valid(form)
        record(self.request, "monitor.create", target=self.object.name, kind=self.object.kind,
               where=self.object.where)  # fmt: skip
        messages.success(
            self.request,
            f"Waiting for the first ping from {self.object.name}."
            if self.object.is_heartbeat
            else f"Monitoring {self.object.name}. First check runs now.",
        )
        return response

    def get_success_url(self):
        return reverse("monitors:detail", args=[self.object.pk])


class MonitorUpdateView(StaffViewMixin, UpdateView):
    permission_required = "monitors.change_monitor"
    template_name = "monitors/form.html"
    form_class = MonitorForm
    model = Monitor
    extra_context = {"sections": FORM_SECTIONS}

    def form_valid(self, form):
        if not form.instance.is_heartbeat and set(form.changed_data) - {
            "name",
            "server",
            "is_enabled",
        }:
            form.instance.next_check_at = timezone.now()
            form.instance.tls_checked_at = None
        if "is_enabled" in form.changed_data and not form.instance.is_enabled:
            checker.resolve_all(form.instance)
        response = super().form_valid(form)
        record(self.request, "monitor.update", target=self.object.name, fields=form.changed_data)
        messages.success(self.request, "Monitor saved.")
        return response

    def get_success_url(self):
        return reverse("monitors:detail", args=[self.object.pk])


class MonitorDeleteView(StaffViewMixin, DeleteView):
    permission_required = "monitors.delete_monitor"
    template_name = "confirm_delete.html"
    model = Monitor
    success_url = reverse_lazy("monitors:list")
    extra_context = {
        "delete_note": "This removes the monitor, its check history and its alerts.",
        "delete_label": "Delete monitor",
    }

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            cancel_url=reverse("monitors:detail", args=[self.object.pk]), **kwargs
        )

    def form_valid(self, form):
        record(self.request, "monitor.delete", target=self.object.name)
        messages.success(self.request, f"Stopped monitoring {self.object.name}.")
        return super().form_valid(form)


class MonitorCheckNowView(StaffViewMixin, View):
    permission_required = "monitors.change_monitor"
    http_method_names = ["post"]

    def post(self, request, pk):
        monitor = get_object_or_404(Monitor, pk=pk)
        if monitor.is_heartbeat:
            messages.info(
                request, f"{monitor.name} is checked when its job pings: run it on the server."
            )
            return redirect(safe_next(request, reverse("monitors:detail", args=[pk])))
        monitor = checker.check(monitor)
        verdict = "up" if monitor.last_error == "" else f"failing: {monitor.last_error}"
        (messages.success if not monitor.last_error else messages.warning)(
            request, f"{monitor.name} is {verdict}."
        )
        return redirect(safe_next(request, reverse("monitors:detail", args=[pk])))


class MonitorToggleView(StaffViewMixin, View):
    permission_required = "monitors.change_monitor"
    http_method_names = ["post"]

    def post(self, request, pk):
        monitor = get_object_or_404(Monitor, pk=pk)
        monitor.is_enabled = not monitor.is_enabled
        if monitor.is_enabled:
            monitor.next_check_at = timezone.now()
        else:
            checker.resolve_all(monitor)
        monitor.save(update_fields=["is_enabled", "next_check_at"])
        record(request, "monitor.resume" if monitor.is_enabled else "monitor.pause",
               target=monitor.name)  # fmt: skip
        messages.success(
            request, f"{monitor.name} {'resumed' if monitor.is_enabled else 'paused'}."
        )
        return redirect(safe_next(request, reverse("monitors:detail", args=[pk])))
