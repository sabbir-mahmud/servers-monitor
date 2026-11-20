"""Scripts kept in the dashboard and run on servers as jobs (see models.Script)."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import DeleteView, FormView, ListView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin
from apps.fleet.models import Server
from apps.tenants.scoping import AccountScopedMixin, get_scoped_or_404, scoped

from . import services
from .forms import ScriptForm, ScriptRunForm
from .models import Job, Script

PERM = "operations.run_shell"  # a script is shell: Owner and Admin


def _shell_counts(request) -> tuple[int, int]:
    servers = list(scoped(request, Server).filter(is_enabled=True).only("capabilities"))
    return sum(1 for s in servers if s.shell_enabled), len(servers)


class ScriptListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = PERM
    template_name = "operations/script_list.html"
    model = Script
    context_object_name = "scripts"

    def get_queryset(self):
        return super().get_queryset().select_related("updated_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["shell_on"], ctx["server_total"] = _shell_counts(self.request)
        last = {}
        for job in scoped(self.request, Job).filter(action="shell", args__has_key="script")\
                .order_by("-created_at").only("args", "created_at")[:200]:  # fmt: skip
            last.setdefault(job.args.get("script"), job)
        for s in ctx["scripts"]:
            s.last_job = last.get(s.name)
        return ctx


class _ScriptFormView(StaffViewMixin, AccountScopedMixin, FormView):
    permission_required = PERM
    template_name = "operations/script_form.html"
    form_class = ScriptForm
    instance = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.instance
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["object"] = self.instance
        ctx["shell_on"], ctx["server_total"] = _shell_counts(self.request)
        return ctx

    def form_valid(self, form):
        script = form.save(commit=False)
        if script.created_by_id is None:
            script.created_by = self.request.user
        script.updated_by = self.request.user
        script.save()
        record(self.request, "script.save", target=script.name, interpreter=script.interpreter,
               lines=script.line_count)  # fmt: skip
        messages.success(self.request, f"Saved “{script.name}”.")
        if self.request.POST.get("then") == "run":
            return redirect("operations:script_run", pk=script.pk)
        return redirect("operations:script_list")


class ScriptCreateView(_ScriptFormView):
    pass


class ScriptUpdateView(_ScriptFormView):
    def dispatch(self, request, *args, **kwargs):
        self.instance = get_scoped_or_404(request, Script, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)


class ScriptDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = PERM
    model = Script
    template_name = "confirm_delete.html"

    def get_success_url(self):
        return reverse("operations:script_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(delete_note="Jobs it already ran stay in the history, with their output.",
                   delete_label="Delete script",
                   cancel_url=reverse("operations:script_update", args=[self.object.pk]))  # fmt: skip
        return ctx

    def form_valid(self, form):
        record(self.request, "script.delete", target=self.object.name)
        return super().form_valid(form)


class ScriptRunView(StaffViewMixin, AccountScopedMixin, FormView):
    """Pick servers and run a script on them as one job."""

    permission_required = PERM
    template_name = "operations/script_run.html"
    form_class = ScriptRunForm

    def dispatch(self, request, *args, **kwargs):
        self.script = get_scoped_or_404(request, Script, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {"servers": self.request.GET.getlist("server")}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["script"] = self.script
        servers = list(ctx["form"].fields["servers"].queryset)
        ctx["servers"] = servers
        ctx["runnable"] = sum(1 for s in servers if s.shell_enabled)
        chosen = ctx["form"]["servers"].value() or []
        ctx["chosen"] = {str(v) for v in chosen}
        return ctx

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            job, skipped = services.create_job(
                self.request, "shell",
                {"command": self.script.command(), "script": self.script.name},
                list(data["servers"]), note=data.get("note", ""),
            )  # fmt: skip
        except services.JobError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        for server, reason in skipped:
            messages.warning(self.request, f"Skipped {server.name}: {reason}")
        messages.success(
            self.request, f"Running “{self.script.name}” on {job.commands.count()} server(s)."
        )
        return redirect("operations:job_detail", pk=job.pk)
