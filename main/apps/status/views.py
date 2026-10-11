from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csp import csp_override
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin

from .forms import IncidentForm, IncidentUpdateForm, StatusPageForm
from .models import IncidentStatus, StatusIncident, StatusIncidentUpdate, StatusPage
from .render import as_zip, render, snapshot


class StatusPageListView(StaffViewMixin, ListView):
    permission_required = "status.view_statuspage"
    template_name = "status/list.html"
    model = StatusPage
    context_object_name = "pages"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("entries", "incidents")


class StatusPageDetailView(StaffViewMixin, DetailView):
    permission_required = "status.view_statuspage"
    template_name = "status/detail.html"
    model = StatusPage
    context_object_name = "page"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page = self.object
        ctx.update(
            snap=snapshot(page),
            active=page.incidents.filter(resolved_at__isnull=True).prefetch_related(
                "updates", "affected"
            ),
            past=page.incidents.filter(resolved_at__isnull=False).prefetch_related("updates")[:10],
            incident_form=IncidentForm(page=page),
            update_form=IncidentUpdateForm(),
            can_incident=self.request.user.has_perm("status.add_statusincident"),
            can_edit=self.request.user.has_perm("status.change_statuspage"),
        )
        return ctx


class _PageFormMixin:
    model = StatusPage
    form_class = StatusPageForm
    template_name = "status/form.html"

    def get_success_url(self):
        return reverse("status:detail", args=[self.object.pk])


class StatusPageCreateView(StaffViewMixin, _PageFormMixin, CreateView):
    permission_required = "status.add_statuspage"

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        record(self.request, "status_page.create", target=self.object.name)
        messages.success(self.request, f"Status page {self.object.name} created.")
        return response


class StatusPageUpdateView(StaffViewMixin, _PageFormMixin, UpdateView):
    permission_required = "status.change_statuspage"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(
            self.request, "status_page.update", target=self.object.name, fields=form.changed_data
        )
        messages.success(self.request, "Status page saved.")
        return response


class StatusPageDeleteView(StaffViewMixin, DeleteView):
    permission_required = "status.delete_statuspage"
    model = StatusPage
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("status:list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["delete_note"] = (
            "This removes the status page and its incidents. Files already published stay where they are."
        )
        ctx["delete_label"] = "Delete status page"
        ctx["cancel_url"] = reverse("status:detail", args=[self.object.pk])
        return ctx

    def form_valid(self, form):
        record(self.request, "status_page.delete", target=self.object.name)
        messages.success(self.request, f"Deleted {self.object.name}.")
        return super().form_valid(form)


@method_decorator(csp_override({"default-src": ["'none'"], "style-src": ["'unsafe-inline'"],
                                "img-src": ["https:", "data:"], "frame-ancestors": ["'none'"]}), name="get")  # fmt: skip
class StatusPagePreviewView(StaffViewMixin, View):
    """Exactly what would be published, served from here for a look."""

    permission_required = "status.view_statuspage"

    def get(self, request, pk):
        page = get_object_or_404(StatusPage, pk=pk)
        _, html, data = render(page)
        if request.GET.get("format") == "json":
            return HttpResponse(data, content_type="application/json")
        return HttpResponse(html)


class StatusPageDownloadView(StaffViewMixin, View):
    permission_required = "status.view_statuspage"

    def get(self, request, pk):
        page = get_object_or_404(StatusPage, pk=pk)
        response = HttpResponse(as_zip(page), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="status-{page.pk.hex[:8]}.zip"'
        return response


class StatusPagePublishView(StaffViewMixin, View):
    permission_required = "status.change_statuspage"
    http_method_names = ["post"]

    def post(self, request, pk):
        page = get_object_or_404(StatusPage, pk=pk)
        StatusPage.objects.filter(pk=pk).update(needs_publish=True)
        record(request, "status_page.publish", target=page.name)
        messages.success(request, "Publishing within a few seconds (the relay process does it).")
        return redirect("status:detail", pk=pk)


class IncidentCreateView(StaffViewMixin, View):
    permission_required = "status.add_statusincident"
    http_method_names = ["post"]

    def post(self, request, pk):
        page = get_object_or_404(StatusPage, pk=pk)
        form = IncidentForm(request.POST, page=page)
        if not form.is_valid():
            messages.error(request, "Incident not posted: " + "; ".join(
                f"{form.fields[f].label if f in form.fields else f}: {' '.join(e)}" for f, e in form.errors.items()))  # fmt: skip
            return redirect("status:detail", pk=pk)
        with transaction.atomic():
            incident = form.save(commit=False)
            incident.page = page
            incident.save()
            form.save_m2m()
            StatusIncidentUpdate.objects.create(incident=incident, status=incident.status,
                                                message=form.cleaned_data["message"], author=request.user)  # fmt: skip
            StatusPage.objects.filter(pk=pk).update(needs_publish=True)
        record(request, "status_incident.create", target=incident.title, page=page.name)
        messages.success(request, "Incident posted.")
        return redirect("status:detail", pk=pk)


class IncidentUpdateView(StaffViewMixin, View):
    permission_required = "status.change_statusincident"
    http_method_names = ["post"]

    def post(self, request, pk, incident_id):
        incident = get_object_or_404(StatusIncident, pk=incident_id, page_id=pk)
        form = IncidentUpdateForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Write an update message.")
            return redirect("status:detail", pk=pk)
        status = form.cleaned_data["status"]
        with transaction.atomic():
            StatusIncidentUpdate.objects.create(incident=incident, status=status,
                                                message=form.cleaned_data["message"], author=request.user)  # fmt: skip
            incident.status = status
            incident.resolved_at = timezone.now() if status == IncidentStatus.RESOLVED else None
            incident.save(update_fields=["status", "resolved_at"])
            StatusPage.objects.filter(pk=pk).update(needs_publish=True)
        record(request, "status_incident.update", target=incident.title, status=status)
        messages.success(request, "Update posted.")
        return redirect("status:detail", pk=pk)
