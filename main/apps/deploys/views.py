from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DeleteView, DetailView, FormView, ListView

from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin
from apps.tenants.scoping import AccountScopedMixin, get_scoped_or_404

from . import services, webhooks
from .forms import ProjectForm
from .models import OPEN_STATUSES, DeployStatus, Project, Source

MAX_WEBHOOK_BODY = 1024 * 1024


class ProjectListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "deploys.view_project"
    template_name = "deploys/project_list.html"
    model = Project
    context_object_name = "projects"

    def get_queryset(self):
        return (
            super().get_queryset().prefetch_related("servers")
            .annotate(waiting=Count("deployments", filter=Q(deployments__status=DeployStatus.WAITING)))
        )  # fmt: skip

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        for p in ctx["projects"]:
            p.last = p.deployments.select_related("requested_by").first()
        return ctx


class _ProjectFormView(StaffViewMixin, AccountScopedMixin, FormView):
    template_name = "deploys/project_form.html"
    form_class = ProjectForm
    instance = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.instance
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["object"] = self.instance
        return ctx

    def form_valid(self, form):
        project = form.save(commit=False)
        creating = project._state.adding
        if creating:
            project.created_by = self.request.user
            project.webhook_secret = services.new_secret()
        project.save()
        form.save_m2m()
        record(self.request, "deploy.project_save", target=project.name, repo=project.repo_url,
               branch=project.branch, mode=project.mode,
               steps_changed=bool(form.has_changed() and "steps" in form.changed_data))  # fmt: skip
        messages.success(self.request, f"Saved {project.name}.")
        url = reverse("deploys:project", args=[project.pk])
        return redirect(url + ("#setup" if creating else ""))


class ProjectCreateView(_ProjectFormView):
    permission_required = "deploys.add_project"


class ProjectUpdateView(_ProjectFormView):
    permission_required = "deploys.change_project"

    def dispatch(self, request, *args, **kwargs):
        self.instance = get_scoped_or_404(request, Project, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)


class ProjectDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "deploys.delete_project"
    model = Project
    template_name = "confirm_delete.html"

    def get_success_url(self):
        return reverse("deploys:list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(delete_note="Its webhook stops working and its deploy history goes. The "
                               "servers' checkouts and their apps in smctl are left as they are.",
                   delete_label="Delete project",
                   cancel_url=reverse("deploys:project", args=[self.object.pk]))  # fmt: skip
        return ctx

    def form_valid(self, form):
        record(self.request, "deploy.project_delete", target=self.object.name)
        return super().form_valid(form)


class ProjectDetailView(StaffViewMixin, AccountScopedMixin, HtmxTemplateMixin, DetailView):
    permission_required = "deploys.view_project"
    template_name = "deploys/project_detail.html"
    htmx_partial = "history"
    model = Project
    context_object_name = "project"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project = self.object
        deployments = list(project.deployments.select_related("requested_by", "decided_by")[:30])
        ctx.update(
            deployments=deployments,
            has_open=any(d.status in OPEN_STATUSES for d in deployments),
            targets=services.server_commits(project),
            last_good=next((d for d in deployments if d.status == DeployStatus.SUCCEEDED), None),
            can_approve=services.can_approve(self.request.user),
        )
        if self.request.user.has_perm("deploys.change_project") and not self.request.htmx:
            ctx["webhook_url"] = self.request.build_absolute_uri(
                reverse("deploy_webhook", args=[project.pk])
            )
            ctx["webhook_secret"] = services.secret_of(project)
        return ctx


class DeploymentDetailView(StaffViewMixin, HtmxTemplateMixin, DetailView):
    permission_required = "deploys.view_deployment"
    template_name = "deploys/deployment_detail.html"
    htmx_partial = "run"
    context_object_name = "dep"

    def get_object(self, queryset=None):
        project = get_scoped_or_404(self.request, Project, pk=self.kwargs["pk"])
        dep = project.deployments.select_related("job", "requested_by", "decided_by").filter(
            pk=self.kwargs["dep"]
        ).first()  # fmt: skip
        if dep is None:
            raise Http404
        return dep

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        dep = self.object
        ctx["project"] = dep.project
        ctx["commands"] = (
            list(dep.job.commands.select_related("server").order_by("batch", "server__name"))
            if dep.job else []
        )  # fmt: skip
        ctx["can_approve"] = services.can_approve(self.request.user)
        ctx["commit_url"] = dep.project.commit_url(dep.sha)
        return ctx


class DeployNowView(StaffViewMixin, View):
    """Deploy the branch's latest commit, or (sha=) redeploy an earlier one."""

    permission_required = "deploys.add_deployment"
    http_method_names = ["post"]

    def post(self, request, pk):
        project = get_scoped_or_404(request, Project, pk=pk)
        if not project.is_enabled:
            messages.error(request, "This project is turned off.")
            return redirect("deploys:project", pk=pk)
        sha, message, author, source = "", "", "", Source.MANUAL
        if from_pk := request.POST.get("redeploy"):
            try:
                earlier = project.deployments.filter(pk=from_pk).exclude(sha="").first()
            except ValidationError:
                earlier = None
            if earlier is None:
                raise Http404
            sha, message, author, source = (
                earlier.sha,
                earlier.message,
                earlier.author,
                Source.ROLLBACK,
            )
        dep = services.request_deploy(project, source=source, sha=sha, message=message,
                                      author=author, user=request.user, request=request)  # fmt: skip
        if dep.status == DeployStatus.WAITING:
            messages.info(request, "Waiting for an Admin to approve it.")
        elif dep.status == DeployStatus.FAILED:
            messages.error(request, f"Couldn't start: {dep.error}")
        return redirect("deploys:deployment", pk=pk, dep=dep.pk)


class DecideView(StaffViewMixin, View):
    permission_required = "deploys.approve_deployment"
    http_method_names = ["post"]

    def post(self, request, pk, dep, decision):
        project = get_scoped_or_404(request, Project, pk=pk)
        deployment = project.deployments.filter(pk=dep).first()
        if deployment is None or decision not in ("approve", "reject"):
            raise Http404
        if not services.decide(request, deployment, approve=decision == "approve"):
            messages.warning(request, "Someone already decided this one.")
        return redirect("deploys:deployment", pk=pk, dep=dep)


class SecretView(StaffViewMixin, View):
    permission_required = "deploys.change_project"
    http_method_names = ["post"]

    def post(self, request, pk):
        project = get_scoped_or_404(request, Project, pk=pk)
        Project.objects.filter(pk=pk).update(webhook_secret=services.new_secret())
        record(request, "deploy.secret_rotate", target=project.name)
        messages.warning(request, "New secret: paste it into the webhook's settings, or pushes "
                                  "stop deploying.")  # fmt: skip
        return redirect(reverse("deploys:project", args=[pk]) + "#setup")


@csrf_exempt
@require_POST
def webhook(request, pk):
    """Where the git host sends pushes. Public: the signature is the authentication."""
    project = Project.objects.filter(pk=pk, is_enabled=True).select_related("account").first()
    if project is None:
        return JsonResponse({"error": "No such project."}, status=404)
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_WEBHOOK_BODY:
        return JsonResponse({"error": "Too large."}, status=413)
    body = request.body
    try:
        event = webhooks.parse(request.headers, body, services.secret_of(project))
    except webhooks.WebhookError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    if isinstance(event, webhooks.Ignored):
        return JsonResponse({"ignored": event.reason}, status=202)
    if event.branch != project.branch:
        return JsonResponse({"ignored": f"{event.branch} isn't {project.branch}"}, status=202)
    if len(event.sha) != 40 or any(c not in "0123456789abcdef" for c in event.sha):
        return JsonResponse({"error": "No commit id in the push."}, status=400)
    if event.delivery_id and project.deployments.filter(delivery_id=event.delivery_id).exists():
        return JsonResponse({"ignored": "already received"}, status=200)
    dep = services.request_deploy(project, source=Source.PUSH, sha=event.sha,
                                  message=event.message, author=event.author,
                                  delivery_id=event.delivery_id)  # fmt: skip
    return JsonResponse({"deployment": str(dep.pk), "status": dep.status}, status=202)
