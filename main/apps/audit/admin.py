from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor_label", "action", "target", "server", "ip")
    list_filter = ("action",)
    search_fields = ("actor_label", "target")
    list_select_related = ("server",)

    # Append-only: the admin can read the trail but never rewrite it.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
