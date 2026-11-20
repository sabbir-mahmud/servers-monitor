"""
The permission sets behind every role. Roles are memberships of an account
(apps/tenants/roles.py maps owner/admin/guardian/operator/viewer onto these sets and
removes what the account type doesn't offer); Django Groups are not used.

Viewer   — read everything except the audit log.
Operator — Viewer + run allow-listed actions, cancel commands, acknowledge alerts,
           edit/pause/re-check application monitors, schedule maintenance windows,
           post status-page incidents.
Admin    — Operator + manage servers/tokens, monitors, status pages, notification channels and users
           (invite, roles, deactivate, reset passwords), free-form shell, audit log.
"""

import logging

logger = logging.getLogger(__name__)

VIEW = [
    "fleet.view_server",
    "fleet.view_servergroup",
    "operations.view_job",
    "operations.view_command",
    "alerts.view_alert",
    "monitors.view_monitor",
    "alerts.view_maintenancewindow",
    "automations.view_automation",
    "automations.view_run",
]
OPERATE = [
    "operations.add_job",
    "operations.change_command",
    "alerts.change_alert",
    "monitors.change_monitor",
    "alerts.add_maintenancewindow",
    "alerts.change_maintenancewindow",
    "alerts.delete_maintenancewindow",
    "status.add_statusincident",
    "status.change_statusincident",
    "automations.add_run",  # run now, redeploy an earlier commit
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
    "automations.add_automation",
    "automations.change_automation",
    "automations.delete_automation",
    "automations.approve_run",
]

ROLE_MATRIX = {
    "Viewer": VIEW,
    "Operator": VIEW + OPERATE,
    "Admin": VIEW + OPERATE + ADMINISTER,
}
