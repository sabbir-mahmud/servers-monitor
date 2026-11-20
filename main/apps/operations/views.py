from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin
from apps.tenants.scoping import AccountScopedMixin, get_scoped_or_404

from . import services
from .forms import JobForm
from .models import ACTIVE_STATUSES, Command, CommandStatus, Job


class JobListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "operations.view_job"
    template_name = "operations/job_list.html"
    paginate_by = 30
    model = Job

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("created_by", "automation")
            .annotate(
                total=Count("commands"),
                active=Count("commands", filter=Q(commands__status__in=ACTIVE_STATUSES)),
                ok=Count("commands", filter=Q(commands__status=CommandStatus.SUCCEEDED)),
                bad=Count(
                    "commands",
                    filter=Q(
                        commands__status__in=[
                            CommandStatus.FAILED,
                            CommandStatus.LOST,
                            CommandStatus.EXPIRED,
                        ]
                    ),
                ),
            )
            .order_by("-created_at")
        )  # GROUP BY drops Meta.ordering; pagination needs one

    def get_context_data(self, **kwargs):
        from apps.automations.models import Run, RunStatus

        ctx = super().get_context_data(**kwargs)
        ctx["tab"] = "activity"
        ctx["waiting"] = list(
            Run.objects.filter(automation__account=self.request.account, status=RunStatus.WAITING)
            .select_related("automation", "requested_by").order_by("created_at")[:20]
        )  # fmt: skip
        return ctx


class JobCreateView(StaffViewMixin, AccountScopedMixin, FormView):
    permission_required = "operations.add_job"
    template_name = "operations/job_form.html"
    form_class = JobForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        g = self.request.GET
        initial = {k: g[k] for k in ("action", "name") if k in g}
        if servers := g.getlist("server"):
            initial["servers"] = servers
        return initial

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            job, skipped = services.create_job(
                self.request,
                data["action"],
                form.action_args(),
                list(data["servers"]),
                note=data.get("note", ""),
                rollout=form.rollout(),
            )
        except services.JobError as exc:
            if self.request.htmx:
                return render(
                    self.request, "operations/command_detail.html#error", {"error": str(exc)}
                )
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        if self.request.htmx and job.commands.count() == 1:
            # Inline flows on the server page (log viewer, action buttons) swap in the
            # single command's live output instead of navigating away.
            command = job.commands.select_related("server").get()
            return render(
                self.request, "operations/command_detail.html#output", {"command": command}
            )
        for server, reason in skipped:
            messages.warning(self.request, f"Skipped {server.name}: {reason}")
        messages.success(self.request, f"Queued on {job.commands.count()} server(s).")
        return redirect("operations:job_detail", pk=job.pk)


class JobDetailView(StaffViewMixin, AccountScopedMixin, HtmxTemplateMixin, DetailView):
    permission_required = "operations.view_job"
    template_name = "operations/job_detail.html"
    htmx_partial = "commands"
    model = Job

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("created_by", "automation")
            .prefetch_related("commands__server")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["counts"] = services.job_counts(self.object)
        if self.object.strategy == "rolling":
            batches = {}
            for c in self.object.commands.all():
                batches.setdefault(c.batch, []).append(c)
            ctx["batches"] = sorted(batches.items())
        return ctx


class CommandDetailView(StaffViewMixin, AccountScopedMixin, HtmxTemplateMixin, DetailView):
    permission_required = "operations.view_command"
    template_name = "operations/command_detail.html"
    htmx_partial = "output"
    model = Command
    context_object_name = "command"

    def get_queryset(self):
        return super().get_queryset().select_related("server", "job")


class CommandCancelView(StaffViewMixin, View):
    permission_required = "operations.change_command"
    http_method_names = ["post"]

    def post(self, request, pk):
        command = get_scoped_or_404(request, Command.objects.select_related("server"), pk=pk)
        if services.cancel_command(request, command):
            messages.success(request, f"Cancelled on {command.server.name}.")
        else:
            messages.error(request, "Only queued commands can be cancelled.")
        return redirect("operations:job_detail", pk=command.job_id)


class CommandStopView(StaffViewMixin, View):
    """Stop following a log (the server page's Stop button)."""

    permission_required = "operations.change_command"
    http_method_names = ["post"]

    def post(self, request, pk):
        command = get_scoped_or_404(request, Command.objects.select_related("server", "job"), pk=pk)
        if services.stop_follow(request, command):
            command.stopping = True
        return render(request, "operations/command_detail.html#output", {"command": command})


class JobControlView(StaffViewMixin, View):
    """Resume a halted rollout, or abort the batches that haven't started."""

    permission_required = "operations.add_job"
    http_method_names = ["post"]

    def post(self, request, pk, op):
        job = get_scoped_or_404(request, Job, pk=pk)
        if op == "resume" and services.resume_job(request, job):
            messages.success(
                request, "Rollout resumed: the next batch starts once the checks pass."
            )
        elif op == "abort":
            n = services.abort_job(request, job)
            messages.success(request, f"Rollout aborted; {n} server(s) won't run it.")
        return redirect("operations:job_detail", pk=pk)
