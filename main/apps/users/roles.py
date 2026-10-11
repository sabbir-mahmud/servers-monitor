"""
Roles are Django Groups whose permissions are owned by this matrix — the only supported
way to change what a role can do. `sync_roles` runs after every migrate.

Viewer   — read everything except the audit log.
Operator — Viewer + run allow-listed actions, cancel commands, acknowledge alerts,
           edit/pause/re-check application monitors, schedule maintenance windows.
Admin    — Operator + manage servers/tokens, monitors, notification channels and users
           (invite, roles, deactivate, reset passwords), free-form shell, audit log.
"""

import logging

from django.contrib.auth.models import Group, Permission

logger = logging.getLogger(__name__)

VIEW = [
    "fleet.view_server",
    "fleet.view_servergroup",
    "operations.view_job",
    "operations.view_command",
    "alerts.view_alert",
    "monitors.view_monitor",
    "alerts.view_maintenancewindow",
]
OPERATE = [
    "operations.add_job",
    "operations.change_command",
    "alerts.change_alert",
    "monitors.change_monitor",
    "alerts.add_maintenancewindow",
    "alerts.change_maintenancewindow",
    "alerts.delete_maintenancewindow",
]
ADMINISTER = [
    "fleet.add_server",
    "fleet.change_server",
    "fleet.delete_server",
    "fleet.add_servergroup",
    "fleet.change_servergroup",
    "fleet.delete_servergroup",
    "alerts.view_channel",
    "alerts.add_channel",
    "alerts.change_channel",
    "alerts.delete_channel",
    "monitors.add_monitor",
    "monitors.delete_monitor",
    "operations.run_shell",
    "audit.view_auditlog",
    "users.view_user",
    "users.add_user",
    "users.change_user",
]

ROLE_MATRIX = {
    "Viewer": VIEW,
    "Operator": VIEW + OPERATE,
    "Admin": VIEW + OPERATE + ADMINISTER,
}


def sync_roles() -> None:
    for role, perms in ROLE_MATRIX.items():
        group, _ = Group.objects.get_or_create(name=role)
        wanted = []
        for dotted in perms:
            app_label, codename = dotted.split(".")
            perm = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if perm is None:
                # Happens on the first post_migrate before every app's perms exist.
                logger.debug("Permission %s not created yet; skipping", dotted)
                continue
            wanted.append(perm)
        group.permissions.set(wanted)
