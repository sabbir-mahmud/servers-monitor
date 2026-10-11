from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
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

from . import agentdist, services
from .forms import ServerForm
from .models import Environment, Health, Server, ServerGroup

ENROLL_SESSION_KEY = "enroll-token:{}"
MUTE_CHOICES = [(60, "Mute for 1 hour"), (240, "Mute for 4 hours"), (1440, "Mute for 24 hours")]


class DashboardView(StaffViewMixin, HtmxTemplateMixin, ListView):
    """Fleet overview. The `fleet` partial re-renders every 10 s via HTMX polling."""

    permission_required = "fleet.view_server"
    template_name = "fleet/dashboard.html"
    htmx_partial = "fleet"
    context_object_name = "servers"

    def get_queryset(self):
        qs = Server.objects.prefetch_related("groups")
        g = self.request.GET
        if env := g.get("environment"):
            qs = qs.filter(environment=env)
        if group := g.get("group"):
            qs = qs.filter(groups__name=group)
        if q := g.get("q", "").strip():
            qs = qs.filter(name__icontains=q)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        servers = list(ctx["servers"])
        now = timezone.now()
        for s in servers:
            s.current_health = s.health_at(now)
        # Health is derived from last_seen_at, so this filter runs in Python on the
        # already-fetched page rather than as SQL.
        if health := self.request.GET.get("health"):
            servers = [s for s in servers if s.current_health == health]
        ctx.update(
            servers=servers,
            summary=services.fleet_summary(ctx["servers"]),
            open_alerts=Alert.objects.filter(resolved_at__isnull=True).count(),
            environments=Environment.choices,
            groups=ServerGroup.objects.all(),
            healths=Health.choices,
            filters=self.request.GET,
        )
        return ctx


class ServerCreateView(StaffViewMixin, CreateView):
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
        server = get_object_or_404(Server, pk=kwargs["pk"])
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


class ServerDetailView(StaffViewMixin, DetailView):
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
        return Server.objects.prefetch_related("groups")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = self.object
        range_key = self.request.GET.get("range", "1h")
        if range_key not in services.CHART_RANGES:
            range_key = "1h"
        ctx.update(
            range_key=range_key,
            ranges=list(services.CHART_RANGES),
            charts=services.chart_series(server, range_key),
            open_alerts=server.alerts.filter(resolved_at__isnull=True),
            recent_commands=server.commands.select_related("job").order_by("-created_at")[:10],
            can_operate=self.request.user.has_perm("operations.add_job"),
            can_shell=self.request.user.has_perm("operations.run_shell"),
            can_admin=self.request.user.has_perm("fleet.change_server"),
            maintenance_window=maintenance.coverage().window_for_server(server.pk),
            mute_choices=MUTE_CHOICES,
        )
        return ctx


class ServerUpdateView(StaffViewMixin, UpdateView):
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
        messages.success(self.request, "Server updated. The agent picks up the change right away.")
        return response

    def get_success_url(self):
        return reverse("fleet:server_detail", args=[self.object.pk])


class ServerDeleteView(StaffViewMixin, DeleteView):
    permission_required = "fleet.delete_server"
    template_name = "confirm_delete.html"
    model = Server
    success_url = reverse_lazy("fleet:dashboard")

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
        server = get_object_or_404(Server, pk=pk)
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
            targets, batch = [get_object_or_404(Server, pk=pk)], 0
        else:
            targets = [s for s in Server.objects.filter(is_enabled=True) if s.agent_outdated]
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
            return redirect("system" if request.POST.get("back") == "system" else "fleet:dashboard")
        for server, reason in skipped:
            messages.warning(request, f"Skipped {server.name}: {reason}")
        messages.success(
            request, f"Upgrading {job.commands.count()} agent(s) to {job.args['ref']}."
        )
        return redirect("operations:job_detail", pk=job.pk)
