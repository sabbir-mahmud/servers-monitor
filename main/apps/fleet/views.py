from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import format_lazy
from django.views import View
from django.views.generic import DeleteView, DetailView, ListView, TemplateView, UpdateView
from django.views.generic.edit import CreateView

from apps.agent_api.relay import RelayError
from apps.alerts import maintenance
from apps.alerts.models import Alert
from apps.alerts.services import resolve_all
from apps.audit.services import record
from apps.core import crypto
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin
from apps.devices.models import Machine, MachineKind
from apps.tenants.scoping import AccountScopedMixin, get_scoped_or_404, scoped

from . import agentdist, services
from .forms import ServerForm
from .models import Environment, Health, Server, ServerGroup

ENROLL_SESSION_KEY = "enroll-token:{}"
MUTE_CHOICES = [(60, "Mute for 1 hour"), (240, "Mute for 4 hours"), (1440, "Mute for 24 hours")]


class DashboardView(StaffViewMixin, AccountScopedMixin, HtmxTemplateMixin, ListView):
    """Everything the account watches, in one table: servers and, for a solo developer,
    their own computers and phones. One query on the shared Machine table joins each
    row's details. The `fleet` partial re-renders every 10 s via HTMX polling."""

    permission_required = "fleet.view_server"
    template_name = "fleet/dashboard.html"
    htmx_partial = "fleet"
    context_object_name = "machines"
    model = Machine

    def get(self, request, *args, **kwargs):
        # The page is always one list: /devices/?kind=server or ?kind=device. A bare
        # /devices/ (or any other kind) is not a route. Runs after the login,
        # permission and account checks in dispatch().
        allowed = {MachineKind.SERVER} | ({MachineKind.DEVICE} if self.with_devices else set())
        if request.GET.get("kind") not in allowed:
            raise Http404("Pick ?kind=server or ?kind=device.")
        return super().get(request, *args, **kwargs)

    @property
    def with_devices(self) -> bool:
        # A family or company sees computers and phones under People instead.
        return self.request.account.kind == "solo" and self.request.user.has_perm(
            "devices.view_device"
        )

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .select_related("server", "device")
            .prefetch_related("server__groups")
        )
        g = self.request.GET
        qs = qs.filter(kind=g["kind"])
        # Environment and group are server settings: picking one narrows to servers.
        if env := g.get("environment"):
            qs = qs.filter(server__environment=env)
        if group := g.get("group"):
            qs = qs.filter(server__groups__name=group)
        if q := g.get("q", "").strip():
            qs = qs.filter(name__icontains=q)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        rows = []
        for m in ctx["machines"]:
            row = m.details
            # One status vocabulary for the table: a device waiting to pair is "pending".
            if m.kind == MachineKind.SERVER:
                row.status = row.health_at(now)
            else:
                row.status = Health.PENDING if row.state == "waiting" else row.state
            rows.append(row)

        counts = {h.value: 0 for h in Health}
        for r in rows:
            counts[r.status] += 1
        seen = [r.last_seen_at for r in rows if r.last_seen_at]
        low_battery = sum(
            1 for r in rows if r.kind == MachineKind.DEVICE and r.attention == "Battery low"
        )
        overview = {
            **counts,
            "total": len(rows),
            "servers": sum(1 for r in rows if r.kind == MachineKind.SERVER),
            "low_battery": low_battery,
            "attention": counts["offline"] + counts["late"] + low_battery,
            "last_seen": max(seen, default=None),
        }
        # Health is derived from last_seen_at, so this filter runs in Python on the
        # already-fetched rows rather than as SQL.
        if health := self.request.GET.get("health"):
            rows = [r for r in rows if r.status == health]
        all_servers = all_computers = all_phones = 0
        if self.with_devices:  # tab counts: the whole account, whatever is filtered
            from django.db.models import Count, Q

            from apps.devices.models import PHONES

            c = scoped(self.request, Machine).aggregate(
                servers=Count("pk", filter=Q(kind=MachineKind.SERVER)),
                devices=Count("pk", filter=Q(kind=MachineKind.DEVICE)),
                phones=Count("pk", filter=Q(device__platform__in=PHONES)),
            )
            all_servers, all_phones = c["servers"], c["phones"]
            all_computers = c["devices"] - c["phones"]
        kind = self.request.GET["kind"]
        ctx.update(
            rows=rows,
            mix={"servers": all_servers, "computers": all_computers, "phones": all_phones},
            kind=kind,
            overview=overview,
            with_devices=self.with_devices,
            filtering=any(self.request.GET.get(k) for k in ("q", "environment", "group", "health")),
            open_alerts=scoped(self.request, Alert).filter(resolved_at__isnull=True).count(),
            environments=Environment.choices,
            groups=scoped(self.request, ServerGroup),
            healths=Health.choices,
            kinds=MachineKind.choices,
            filters=self.request.GET,
        )
        return ctx


class ServerCreateView(StaffViewMixin, AccountScopedMixin, CreateView):
    permission_required = "fleet.add_server"
    template_name = "fleet/server_form.html"
    form_class = ServerForm

    def form_valid(self, form):
        try:
            server, agent_url = services.create_server(self.request, form)
        except RelayError as exc:
            form.add_error(None, f"Could not reach the relay (Redis): {exc}")
            return self.form_invalid(form)
        # The connection URL waits (encrypted) in this session until the enrolment page
        # shows it once.
        self.request.session[ENROLL_SESSION_KEY.format(server.pk)] = crypto.encrypt(agent_url)
        return redirect("fleet:server_enroll", pk=server.pk)


class ServerEnrollView(StaffViewMixin, TemplateView):
    permission_required = "fleet.add_server"
    template_name = "fleet/server_enroll.html"

    def get_template_names(self):
        # The page polls its connection status until the agent's first heartbeat lands.
        if self.request.htmx and self.request.GET.get("part") == "status":
            return [f"{self.template_name}#status"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = get_scoped_or_404(self.request, Server, pk=kwargs["pk"])
        token = None
        if self.request.GET.get("part") != "status":  # don't burn the one-time token on a poll
            stored = self.request.session.pop(ENROLL_SESSION_KEY.format(server.pk), None)
            token = crypto.decrypt(stored) if stored else None
        try:
            install, upgrade, dist_error = (
                agentdist.install_command(),
                agentdist.upgrade_command(),
                "",
            )
        except agentdist.AgentFilesMissing as exc:
            install = upgrade = ""
            dist_error = str(exc)
        ctx.update(
            server=server,
            agent_url=token,
            install_command=install,
            upgrade_command=upgrade,
            dist_error=dist_error,
            agent_ref=settings.AGENT_REF,
        )
        return ctx


# A day shows today's pattern at a glance; 1 h is one click away.
DEFAULT_CHART_RANGE = "24h"


class ServerDetailView(StaffViewMixin, AccountScopedMixin, DetailView):
    permission_required = "fleet.view_server"
    template_name = "fleet/server_detail.html"
    model = Server
    context_object_name = "server"

    def get_template_names(self):
        part = self.request.GET.get("part")
        if self.request.htmx and part in ("live", "charts"):
            return [f"{self.template_name}#{part}"]
        return [self.template_name]

    def get_queryset(self):
        return super().get_queryset().prefetch_related("groups")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = self.object
        range_key = self.request.GET.get("range", DEFAULT_CHART_RANGE)
        if range_key not in services.CHART_RANGES:
            range_key = DEFAULT_CHART_RANGE
        # Each HTMX refresh only computes the series it renders: History polls `charts`,
        # the metric tiles (always the last hour) poll `live`.
        part = self.request.GET.get("part") if self.request.htmx else None
        charts = trend = None
        if part != "live":
            charts = services.chart_series(server, range_key)
        if part != "charts":
            trend = charts if range_key == "1h" and charts else services.chart_series(server, "1h")
        ctx.update(
            range_key=range_key,
            ranges=list(services.CHART_RANGES),
            charts=charts,
            trend=trend,
            open_alerts=server.alerts.filter(resolved_at__isnull=True),
            recent_commands=server.commands.select_related("job").order_by("-created_at")[:10],
            can_operate=self.request.user.has_perm("operations.add_job"),
            can_shell=self.request.user.has_perm("operations.run_shell"),
            can_admin=self.request.user.has_perm("fleet.change_server"),
            maintenance_window=maintenance.coverage(account=server.account_id).window_for_server(
                server.pk
            ),
            mute_choices=MUTE_CHOICES,
        )
        return ctx


class ServerUpdateView(StaffViewMixin, AccountScopedMixin, UpdateView):
    permission_required = "fleet.change_server"
    template_name = "fleet/server_form.html"
    form_class = ServerForm
    model = Server

    def form_valid(self, form):
        response = super().form_valid(form)
        services.log_settings_change(self.request, self.object, form.changed_data)
        services.server_settings_changed(self.object, form.changed_data)
        if "is_enabled" in form.changed_data and not self.object.is_enabled:
            resolve_all(self.object)
        word = self.request.account.machine_label.capitalize()
        messages.success(self.request, f"{word} updated. The agent picks up the change right away.")
        return response

    def get_success_url(self):
        return reverse("fleet:server_detail", args=[self.object.pk])


class ServerDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "fleet.delete_server"
    template_name = "confirm_delete.html"
    model = Server
    success_url = format_lazy("{}?kind=server", reverse_lazy("fleet:dashboard"))

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            cancel_url=reverse("fleet:server_detail", args=[self.object.pk]), **kwargs
        )

    def form_valid(self, form):
        record(self.request, "server.delete", target=self.object.name)
        services.delete_server(self.object)  # also revokes its relay user
        messages.success(self.request, f"Removed {self.object.name}. Uninstall its agent too.")
        return redirect(self.success_url)


class ServerRotateTokenView(StaffViewMixin, View):
    permission_required = "fleet.change_server"
    http_method_names = ["post"]

    def post(self, request, pk):
        server = get_scoped_or_404(request, Server, pk=pk)
        try:
            agent_url = services.rotate_token(request, server)
        except RelayError as exc:
            messages.error(request, f"Could not reach the relay (Redis): {exc}")
            return redirect("fleet:server_detail", pk=server.pk)
        request.session[ENROLL_SESSION_KEY.format(server.pk)] = crypto.encrypt(agent_url)
        messages.warning(
            request, "Credentials rotated. The old ones no longer work: update the agent."
        )
        return redirect("fleet:server_enroll", pk=server.pk)


class AgentUpgradeView(StaffViewMixin, View):
    """Upgrade one server's agent, or every outdated one in rolling batches (no SSH)."""

    permission_required = "fleet.change_server"
    http_method_names = ["post"]

    def post(self, request, pk=None):
        from apps.operations import services as ops

        if pk is not None:
            targets, batch = [get_scoped_or_404(request, Server, pk=pk)], 0
        else:
            targets = [
                s for s in scoped(request, Server).filter(is_enabled=True) if s.agent_outdated
            ]
            try:
                batch = max(1, min(50, int(request.POST.get("batch_size") or 2)))
            except ValueError:
                batch = 2
        try:
            job, skipped = ops.upgrade_agents(request, targets, batch)
        except ops.JobError as exc:
            messages.error(request, str(exc))
            if pk is not None:
                return redirect("fleet:server_detail", pk=pk)
            if request.POST.get("back") == "system":
                return redirect("system")
            return redirect(reverse("fleet:dashboard") + "?kind=server")
        for server, reason in skipped:
            messages.warning(request, f"Skipped {server.name}: {reason}")
        messages.success(
            request, f"Upgrading {job.commands.count()} agent(s) to {job.args['ref']}."
        )
        return redirect("operations:job_detail", pk=job.pk)
