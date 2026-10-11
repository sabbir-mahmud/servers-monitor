from django.contrib import admin

from .models import Alert


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("opened_at", "server", "kind", "subject", "severity", "resolved_at")
    list_filter = ("kind", "severity")
    search_fields = ("server__name", "message", "subject")
    list_select_related = ("server",)
    readonly_fields = ("opened_at",)
    autocomplete_fields = ("server",)
