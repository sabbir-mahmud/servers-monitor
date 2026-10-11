from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin

from . import services
from .forms import JobForm
from .models import ACTIVE_STATUSES, Command, CommandStatus, Job


class JobListView(StaffViewMixin, ListView):
    permission_required = "operations.view_job"
    template_name = "operations/job_list.html"
    paginate_by = 30

    def get_queryset(self):
        return Job.objects.select_related("created_by").annotate(
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


class JobCreateView(StaffViewMixin, FormView):
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


class JobDetailView(StaffViewMixin, HtmxTemplateMixin, DetailView):
    permission_required = "operations.view_job"
    template_name = "operations/job_detail.html"
    htmx_partial = "commands"
    model = Job

    def get_queryset(self):
        return Job.objects.select_related("created_by").prefetch_related("commands__server")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["counts"] = services.job_counts(self.object)
        return ctx


class CommandDetailView(StaffViewMixin, HtmxTemplateMixin, DetailView):
    permission_required = "operations.view_command"
    template_name = "operations/command_detail.html"
    htmx_partial = "output"
    model = Command
    context_object_name = "command"

    def get_queryset(self):
        return Command.objects.select_related("server", "job")


class CommandCancelView(StaffViewMixin, View):
    permission_required = "operations.change_command"
    http_method_names = ["post"]

    def post(self, request, pk):
        command = get_object_or_404(Command.objects.select_related("server"), pk=pk)
        if services.cancel_command(request, command):
            messages.success(request, f"Cancelled on {command.server.name}.")
        else:
            messages.error(request, "Only queued commands can be cancelled.")
        return redirect("operations:job_detail", pk=command.job_id)
