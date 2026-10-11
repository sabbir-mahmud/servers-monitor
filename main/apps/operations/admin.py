from django.contrib import admin

from .models import Command, Job


class CommandInline(admin.TabularInline):
    model = Command
    extra = 0
    fields = ("server", "status", "exit_code", "delivered_at", "finished_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "created_by", "note")
    list_filter = ("action",)
    list_select_related = ("created_by",)
    readonly_fields = ("action", "args", "created_by", "created_at")
    inlines = [CommandInline]


@admin.register(Command)
class CommandAdmin(admin.ModelAdmin):
    list_display = ("created_at", "server", "action", "status", "exit_code")
    list_filter = ("status", "action")
    search_fields = ("server__name",)
    list_select_related = ("server",)
    readonly_fields = [f.name for f in Command._meta.fields]
