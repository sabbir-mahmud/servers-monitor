from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.functional import cached_property
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DeleteView, DetailView, FormView, ListView

from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin
from apps.operations.models import ACTIVE_STATUSES, CommandStatus
from apps.tenants.scoping import AccountScopedMixin, get_scoped_or_404, scoped

from . import services, webhooks
from . import templates as presets
from .forms import AutomationForm, RunForm
from .models import OPEN_STATUSES, Automation, Kind, Run, RunStatus, Trigger

MAX_WEBHOOK_BODY = 1024 * 1024
RECENT = 12  # runs in a row's history strip
STRIP = 30  # ... and on an automation's page
KIND_FILTERS = {"action": Kind.ACTION, "script": Kind.SCRIPT, "deploy": Kind.DEPLOY}


def may_run(user, automation: Automation) -> bool:
    if not user.has_perm("automations.add_run"):
        return False
    return not automation.needs_shell or user.has_perm("operations.run_shell")


class AutomationListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "automations.view_automation"
    template_name = "automations/list.html"
    model = Automation
    context_object_name = "automations"

    def get_queryset(self):
        qs = (
            super().get_queryset().prefetch_related("servers", "groups")
            .annotate(waiting=Count("runs", filter=Q(runs__status=RunStatus.WAITING)))
        )  # fmt: skip
        self.kind = self.request.GET.get("kind", "")
        if self.kind in KIND_FILTERS:
            qs = qs.filter(kind=KIND_FILTERS[self.kind])
        self.when = self.request.GET.get("when", "")
        if self.when == "scheduled":
            qs = qs.exclude(cron="")
        elif self.when == "push":
            qs = qs.filter(on_push=True)
        elif self.when == "manual":
            qs = qs.filter(cron="", on_push=False)
        if q := self.request.GET.get("q", "").strip():
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        items = list(ctx["automations"])
        # The last few runs of each, in one query: the history strip on each row.
        recent: dict = {}
        runs = Run.objects.filter(automation__in=items).order_by("-created_at").only(
            "automation_id", "status", "created_at", "finished_at", "started_at", "trigger",
            "message", "sha",
        )  # fmt: skip
        for run in runs[: len(items) * 40]:
            bucket = recent.setdefault(run.automation_id, [])
            if len(bucket) < RECENT:
                bucket.append(run)
        for a in items:
            a.recent = recent.get(a.pk, [])
            a.last = a.recent[0] if a.recent else None
            a.history = list(reversed(a.recent))  # oldest -> newest, left to right
            a.pad = range(RECENT - len(a.recent))
            a.can_run = may_run(user, a)
            a.target_count = len({s.pk for s in a.servers.all()})
        counts = dict(
            Automation.objects.filter(account=self.request.account)
            .values_list("kind").annotate(n=Count("pk"))
        )  # fmt: skip
        everything = Automation.objects.filter(account=self.request.account)
        ctx.update(
            kind=self.kind, when=self.when, q=self.request.GET.get("q", ""), kind_counts=counts,
            total=sum(counts.values()), tab="automations",
            waiting=Run.objects.filter(automation__in=everything, status=RunStatus.WAITING)
            .select_related("automation").order_by("created_at"),
            failing=[a for a in items if a.last and a.last.status == RunStatus.FAILED],
        )  # fmt: skip
        return ctx


class _AutomationFormView(StaffViewMixin, AccountScopedMixin, FormView):
    template_name = "automations/form.html"
    form_class = AutomationForm
    instance = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(instance=self.instance, user=self.request.user)
        return kwargs

    def get_initial(self):
        if self.instance is not None:
            return {}
        g = self.request.GET
        initial = {
            "kind": g.get("kind") if g.get("kind") in KIND_FILTERS else Kind.ACTION,
            "branch": "main",
        }
        if g.get("action"):
            initial["action"] = g["action"]
        if t := self.template:
            initial.update(kind=t.kind, name=self._free_name(t.title), description=t.blurb,
                           **t.initial)  # fmt: skip
        if servers := g.getlist("server"):
            initial["servers"] = servers
        return initial

    @cached_property
    def template(self):
        t = presets.BY_KEY.get(self.request.GET.get("template", ""))
        return t if t and self.instance is None and presets.usable(t, self.request.user) else None

    def _free_name(self, title: str) -> str:
        """The template's title, numbered if the account already has one by that name."""
        taken = set(scoped(self.request, Automation).filter(name__startswith=title)
                    .values_list("name", flat=True))  # fmt: skip
        name, n = title, 2
        while name in taken:
            name, n = f"{title} {n}", n + 1
        return name

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["object"] = self.instance
        ctx["can_shell"] = self.request.user.has_perm("operations.run_shell")
        ctx["preset"] = self.template
        return ctx

    def form_valid(self, form):
        creating = self.instance is None
        a = form.save(commit=False)
        if creating:
            a.account, a.created_by = self.request.account, self.request.user
        a.updated_by = self.request.user
        a.save()
        form.save_m2m()
        record(self.request, "automation.save", target=a.name, kind=a.kind, cron=a.cron,
               on_push=a.on_push, approval=a.require_approval,
               changed=sorted(form.changed_data))  # fmt: skip
        msg = f"Saved {a.name}."
        if a.next_run_at:
            msg += f" Next run: {a.next_run_at:%b %d %H:%M %Z}."
        messages.success(self.request, msg)
        url = reverse("automations:detail", args=[a.pk])
        return redirect(url + ("#setup" if creating and a.kind == Kind.DEPLOY else ""))


class AutomationCreateView(_AutomationFormView):
    permission_required = "automations.add_automation"

    def get(self, request, *args, **kwargs):
        # A bare "New automation" opens the template gallery; any choice opens the form.
        if not request.GET:
            return render(request, "automations/templates.html", {
                "categories": presets.grouped(request.user), "tab": "automations",
                "kind_choices": [(k, label) for k, label in Kind.choices
                                 if k != Kind.SCRIPT or request.user.has_perm("operations.run_shell")],
            })  # fmt: skip
        return super().get(request, *args, **kwargs)


class AutomationUpdateView(_AutomationFormView):
    permission_required = "automations.change_automation"

    def dispatch(self, request, *args, **kwargs):
        self.instance = get_scoped_or_404(request, Automation, pk=kwargs["pk"])
        if (self.instance.needs_shell and request.user.is_authenticated
                and not request.user.has_perm("operations.run_shell")):  # fmt: skip
            raise PermissionDenied("Only Owners and Admins can edit scripts and shell commands.")
        return super().dispatch(request, *args, **kwargs)


class AutomationDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "automations.delete_automation"
    model = Automation
    template_name = "confirm_delete.html"

    def get_success_url(self):
        return reverse("automations:list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        note = "Its future runs stop. Jobs it already ran stay in Activity, with their output."
        if self.object.kind == Kind.DEPLOY:
            note += " Its webhook stops working; the servers' checkouts are left as they are."
        ctx.update(delete_note=note, delete_label="Delete automation",
                   cancel_url=reverse("automations:detail", args=[self.object.pk]))  # fmt: skip
        return ctx

    def form_valid(self, form):
        record(self.request, "automation.delete", target=self.object.name)
        return super().form_valid(form)


class AutomationDetailView(StaffViewMixin, AccountScopedMixin, HtmxTemplateMixin, DetailView):
    permission_required = "automations.view_automation"
    template_name = "automations/detail.html"
    htmx_partial = "history"
    model = Automation
    context_object_name = "a"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a, user = self.object, self.request.user
        runs = list(a.runs.select_related("requested_by", "decided_by", "job")[:30])
        ctx.update(
            runs=runs,
            has_open=any(r.status in OPEN_STATUSES for r in runs),
            targets=list(a.targets()),
            can_approve=services.can_approve(user),
            can_run=may_run(user, a),
            can_edit=user.has_perm("automations.change_automation")
            and (not a.needs_shell or user.has_perm("operations.run_shell")),
        )
        ctx.update(_run_stats(runs))
        if a.kind == Kind.DEPLOY:
            ctx["commits"] = services.server_commits(a)
            ctx["last_good"] = next((r for r in runs if r.status == RunStatus.SUCCEEDED), None)
            if user.has_perm("automations.change_automation") and not self.request.htmx:
                ctx["webhook_url"] = self.request.build_absolute_uri(
                    reverse("automation_webhook", args=[a.pk])
                )
                ctx["webhook_secret"] = services.secret_of(a)
        return ctx


def _run_stats(runs: list) -> dict:
    """Health at a glance, from the most recent runs (newest first)."""
    done = [r for r in runs if r.status in (RunStatus.SUCCEEDED, RunStatus.FAILED)]
    ok = sum(r.status == RunStatus.SUCCEEDED for r in done)
    times = sorted(r.finished_at - r.started_at for r in done if r.started_at and r.finished_at)
    strip = list(reversed(runs[:STRIP]))
    return {
        "last_run": runs[0] if runs else None,
        "success_rate": round(100 * ok / len(done)) if done else None,
        "finished_count": len(done),
        "typical": times[len(times) // 2] if times else None,
        "strip": strip,
        "strip_pad": range(STRIP - len(strip)),
    }


class RunDetailView(StaffViewMixin, HtmxTemplateMixin, DetailView):
    permission_required = "automations.view_automation"
    template_name = "automations/run.html"
    htmx_partial = "run"
    context_object_name = "run"

    def get_object(self, queryset=None):
        a = get_scoped_or_404(self.request, Automation, pk=self.kwargs["pk"])
        run = a.runs.select_related("job", "requested_by", "decided_by").filter(
            pk=self.kwargs["run"]
        ).first()  # fmt: skip
        if run is None:
            raise Http404
        return run

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        run = self.object
        a = run.automation
        ctx["a"] = a
        commands = (
            list(run.job.commands.select_related("server").order_by("batch", "server__name"))
            if run.job else []
        )  # fmt: skip
        bad = {CommandStatus.FAILED, CommandStatus.LOST, CommandStatus.EXPIRED}
        # Failures first: they're what you came to read.
        commands.sort(key=lambda c: (c.status not in bad, c.batch, c.server.name))
        ctx["commands"] = commands
        ctx["ok_count"] = sum(c.status == CommandStatus.SUCCEEDED for c in commands)
        ctx["bad_count"] = sum(c.status in bad for c in commands)
        ctx["active_count"] = sum(c.status in ACTIVE_STATUSES or c.status == CommandStatus.WAITING
                                  for c in commands)  # fmt: skip
        ctx["can_approve"] = services.can_approve(self.request.user)
        ctx["can_run"] = may_run(self.request.user, a)
        ctx["commit_url"] = a.commit_url(run.sha) if a.kind == Kind.DEPLOY else ""
        return ctx


class RunNowView(StaffViewMixin, AccountScopedMixin, FormView):
    """Start a run by hand: on the automation's servers, or on servers chosen here. With
    redeploy=<run>, a git deploy goes back to that run's commit."""

    permission_required = "automations.add_run"
    template_name = "automations/run_form.html"
    form_class = RunForm

    def dispatch(self, request, *args, **kwargs):
        self.automation = get_scoped_or_404(request, Automation, pk=kwargs["pk"])
        if request.user.is_authenticated and not may_run(request.user, self.automation):
            raise PermissionDenied("Only Owners and Admins can run scripts and shell commands.")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["automation"] = self.automation
        return kwargs

    def get_initial(self):
        return {"servers": self.request.GET.getlist("server")}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["a"] = self.automation
        ctx["targets"] = list(self.automation.targets())
        chosen = ctx["form"]["servers"].value() or []
        ctx["chosen"] = {str(v) for v in chosen}
        return ctx

    def form_valid(self, form):
        a = self.automation
        if not a.is_enabled:
            messages.error(self.request, "This automation is turned off.")
            return redirect("automations:detail", pk=a.pk)
        trigger, sha, message, author = Trigger.MANUAL, "", "", ""
        if from_pk := self.request.POST.get("redeploy"):
            try:
                earlier = a.runs.filter(pk=from_pk).exclude(sha="").first()
            except ValidationError:
                earlier = None
            if earlier is None or a.kind != Kind.DEPLOY:
                raise Http404
            trigger, sha, message, author = (Trigger.REDEPLOY, earlier.sha, earlier.message,
                                             earlier.author)  # fmt: skip
        run = services.request_run(
            a, trigger=trigger, user=self.request.user, request=self.request,
            servers=list(form.cleaned_data.get("servers") or []),
            note=form.cleaned_data.get("note", ""), sha=sha, message=message, author=author,
        )  # fmt: skip
        if run.status == RunStatus.WAITING:
            messages.info(self.request, "Waiting for an Admin to approve it.")
        elif run.status == RunStatus.FAILED:
            messages.error(self.request, f"Couldn't start: {run.error}")
        elif run.status == RunStatus.QUEUED:
            messages.info(self.request, "Queued: it starts when the current run finishes.")
        return redirect("automations:run", pk=a.pk, run=run.pk)

    def form_invalid(self, form):
        if self.request.POST.get("quick"):  # the detail page's one-click button
            messages.error(self.request, " ".join(form.non_field_errors()) or "Choose servers.")
            return redirect(reverse("automations:run_now", args=[self.automation.pk]))
        return super().form_invalid(form)


class DecideView(StaffViewMixin, View):
    permission_required = "automations.approve_run"
    http_method_names = ["post"]

    def post(self, request, pk, run, decision):
        a = get_scoped_or_404(request, Automation, pk=pk)
        r = a.runs.filter(pk=run).first()
        if r is None or decision not in ("approve", "reject"):
            raise Http404
        if not services.decide(request, r, approve=decision == "approve"):
            messages.warning(request, "Someone already decided this one.")
        return redirect("automations:run", pk=pk, run=run)


class SecretView(StaffViewMixin, View):
    permission_required = "automations.change_automation"
    http_method_names = ["post"]

    def post(self, request, pk):
        a = get_scoped_or_404(request, Automation, pk=pk, kind=Kind.DEPLOY)
        Automation.objects.filter(pk=pk).update(webhook_secret=services.new_secret())
        record(request, "automation.secret_rotate", target=a.name)
        messages.warning(request, "New secret: paste it into the webhook's settings, or pushes "
                                  "stop deploying.")  # fmt: skip
        return redirect(reverse("automations:detail", args=[pk]) + "#setup")


@csrf_exempt
@require_POST
def webhook(request, pk):
    """Where the git host sends pushes. Public: the signature is the authentication."""
    a = (
        Automation.objects.filter(pk=pk, kind=Kind.DEPLOY, on_push=True, is_enabled=True)
        .select_related("account").first()
    )  # fmt: skip
    if a is None:
        return JsonResponse({"error": "No such automation, or it doesn't deploy on push."},
                            status=404)  # fmt: skip
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_WEBHOOK_BODY:
        return JsonResponse({"error": "Too large."}, status=413)
    try:
        event = webhooks.parse(request.headers, request.body, services.secret_of(a))
    except webhooks.WebhookError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    if isinstance(event, webhooks.Ignored):
        return JsonResponse({"ignored": event.reason}, status=202)
    if event.branch != a.branch:
        return JsonResponse({"ignored": f"{event.branch} isn't {a.branch}"}, status=202)
    if len(event.sha) != 40 or any(c not in "0123456789abcdef" for c in event.sha):
        return JsonResponse({"error": "No commit id in the push."}, status=400)
    if event.delivery_id and a.runs.filter(delivery_id=event.delivery_id).exists():
        return JsonResponse({"ignored": "already received"}, status=200)
    run = services.request_run(a, trigger=Trigger.PUSH, sha=event.sha, message=event.message,
                               author=event.author, delivery_id=event.delivery_id)  # fmt: skip
    return JsonResponse({"run": str(run.pk), "status": run.status}, status=202)
