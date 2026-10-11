from django.views.generic import ListView

from apps.core.mixins import StaffViewMixin

from .models import AuditLog


class AuditLogListView(StaffViewMixin, ListView):
    permission_required = "audit.view_auditlog"
    template_name = "audit/list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = AuditLog.objects.select_related("server")
        if action := self.request.GET.get("action"):
            qs = qs.filter(action=action)
        return qs
