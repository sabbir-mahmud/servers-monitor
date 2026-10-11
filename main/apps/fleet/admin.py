from django.contrib import admin

from .models import MetricSample, Server, ServerGroup


@admin.register(ServerGroup)
class ServerGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name",)


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ("name", "environment", "is_enabled", "last_seen_at", "agent_version")
    list_filter = ("environment", "is_enabled", "groups")
    search_fields = ("name", "hostname")
    filter_horizontal = ("groups",)
    readonly_fields = (
        "token_prefix",
        "hostname",
        "platform",
        "agent_version",
        "last_seen_at",
        "remote_addr",
        "capabilities",
        "services",
        "disks",
        "created_at",
        "updated_at",
    )
    exclude = ("token_hash",)

    def has_add_permission(self, request):
        # Servers are created in the dashboard so the token is generated and shown once.
        return False


@admin.register(MetricSample)
class MetricSampleAdmin(admin.ModelAdmin):
    list_display = ("recorded_at", "server", "cpu_percent", "memory_percent", "disk_percent")
    list_select_related = ("server",)
    list_filter = ("server",)
