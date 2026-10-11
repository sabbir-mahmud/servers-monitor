from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import ListView

from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin

from .models import Alert


class AlertListView(StaffViewMixin, HtmxTemplateMixin, ListView):
    permission_required = "alerts.view_alert"
    template_name = "alerts/list.html"
    htmx_partial = "rows"
    paginate_by = 50

    def get_queryset(self):
        qs = Alert.objects.select_related("server", "acknowledged_by")
        state = self.request.GET.get("state", "open")
        if state == "open":
            qs = qs.filter(resolved_at__isnull=True)
        elif state == "resolved":
            qs = qs.filter(resolved_at__isnull=False)
        if q := self.request.GET.get("q", "").strip():
            qs = qs.filter(Q(server__name__icontains=q) | Q(message__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["state"] = self.request.GET.get("state", "open")
        return ctx


class AlertAcknowledgeView(StaffViewMixin, View):
    permission_required = "alerts.change_alert"
    http_method_names = ["post"]

    def post(self, request, pk):
        alert = get_object_or_404(Alert.objects.select_related("server"), pk=pk)
        if alert.acknowledged_at is None:
            alert.acknowledged_by, alert.acknowledged_at = request.user, timezone.now()
            alert.save(update_fields=["acknowledged_by", "acknowledged_at"])
            record(request, "alert.acknowledge", target=str(alert), server=alert.server)
            messages.success(request, "Alert acknowledged.")
        return redirect(request.POST.get("next") or "alerts:list")
