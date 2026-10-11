from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DeleteView, DetailView, FormView, ListView

from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin

from . import services
from .forms import JobForm, ScheduleForm
from .models import ACTIVE_STATUSES, Command, CommandStatus, Job, Schedule


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
        if self.object.strategy == "rolling":
            batches = {}
            for c in self.object.commands.all():
                batches.setdefault(c.batch, []).append(c)
            ctx["batches"] = sorted(batches.items())
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


class CommandStopView(StaffViewMixin, View):
    """Stop following a log (the server page's Stop button)."""

    permission_required = "operations.change_command"
    http_method_names = ["post"]

    def post(self, request, pk):
        command = get_object_or_404(Command.objects.select_related("server", "job"), pk=pk)
        if services.stop_follow(request, command):
            command.stopping = True
        return render(request, "operations/command_detail.html#output", {"command": command})


class JobControlView(StaffViewMixin, View):
    """Resume a halted rollout, or abort the batches that haven't started."""

    permission_required = "operations.add_job"
    http_method_names = ["post"]

    def post(self, request, pk, op):
        job = get_object_or_404(Job, pk=pk)
        if op == "resume" and services.resume_job(request, job):
            messages.success(
                request, "Rollout resumed: the next batch starts once the checks pass."
            )
        elif op == "abort":
            n = services.abort_job(request, job)
            messages.success(request, f"Rollout aborted; {n} server(s) won't run it.")
        return redirect("operations:job_detail", pk=pk)


class ScheduleListView(StaffViewMixin, ListView):
    permission_required = "operations.view_schedule"
    template_name = "operations/schedule_list.html"
    model = Schedule
    context_object_name = "schedules"

    def get_queryset(self):
        return Schedule.objects.prefetch_related("servers", "groups")


class _ScheduleFormView(StaffViewMixin, FormView):
    template_name = "operations/schedule_form.html"
    form_class = ScheduleForm
    instance = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["instance"] = self.instance
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["object"] = self.instance
        return ctx

    def form_valid(self, form):
        sched = form.save(self.request.user)
        record(self.request, "schedule.save", target=sched.name, cron=sched.cron, runs=sched.action)
        messages.success(
            self.request,
            f"Saved. Next run: {sched.next_run_at:%b %d %H:%M %Z}."
            if sched.next_run_at
            else "Saved (disabled).",
        )
        return redirect("operations:schedule_list")


class ScheduleCreateView(_ScheduleFormView):
    permission_required = "operations.add_schedule"


class ScheduleUpdateView(_ScheduleFormView):
    permission_required = "operations.change_schedule"

    def dispatch(self, request, *args, **kwargs):
        self.instance = get_object_or_404(Schedule, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)


class ScheduleDeleteView(StaffViewMixin, DeleteView):
    permission_required = "operations.delete_schedule"
    model = Schedule
    template_name = "confirm_delete.html"

    def get_success_url(self):
        return reverse("operations:schedule_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(delete_note="Stops future runs. Jobs it already started stay in the history.",
                   delete_label="Delete schedule", cancel_url=reverse("operations:schedule_list"))  # fmt: skip
        return ctx

    def form_valid(self, form):
        record(self.request, "schedule.delete", target=self.object.name)
        return super().form_valid(form)
