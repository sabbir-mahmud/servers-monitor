"""What each account type offers and what each role in it may do.

Permissions are evaluated per membership, never globally: a user can own a family
account and be a Viewer in a company account, and their rights in one never leak into
the other. The permission strings are the usual Django `app_label.codename` ones, so
`PermissionRequiredMixin`, `user.has_perm()` and `{{ perms }}` keep working unchanged;
the backend in permissions.py answers them from the active membership.

A permission is granted only if the role has it **and** the account type has the
feature it belongs to. A family owner is an "Admin" of their account, but never gets
`operations.run_shell`, because family accounts have no servers to run anything on.
"""

from functools import cache

from django.db import models

from apps.users.roles import ADMINISTER, OPERATE, VIEW


class Kind(models.TextChoices):
    FAMILY = "family", "Parent"
    SOLO = "solo", "Solo developer"
    COMPANY = "company", "Company"


KIND_HELP = {
    Kind.FAMILY: "Keep an eye on your children's phones, tablets and computers: last online, "
    "last location, battery.",
    Kind.SOLO: "Your own devices plus servers, websites, API endpoints and custom commands.",
    Kind.COMPANY: "Everything a solo developer gets, plus a team with roles and staff whose "
    "devices you look after.",
}


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    GUARDIAN = "guardian", "Guardian"
    OPERATOR = "operator", "Operator"
    VIEWER = "viewer", "Viewer"


ROLES_BY_KIND = {
    Kind.FAMILY: [Role.OWNER, Role.GUARDIAN, Role.VIEWER],
    Kind.SOLO: [Role.OWNER],
    Kind.COMPANY: [Role.OWNER, Role.ADMIN, Role.OPERATOR, Role.VIEWER],
}

ROLE_HELP = {
    Role.OWNER: "Everything, including the account itself. Every account keeps at least one.",
    Role.ADMIN: "Everything except the account itself: servers, credentials, members, "
    "notifications, shell, audit log.",
    Role.GUARDIAN: "A second parent: sees and manages everything, invites family members.",
    Role.OPERATOR: "Sees everything except the audit log; runs actions, acknowledges alerts, "
    "mutes and edits apps.",
    Role.VIEWER: "Sees everything except the audit log. Can't change anything.",
}

# Roles that manage the account's people and settings (the old "Admin").
MANAGER_ROLES = {Role.OWNER, Role.ADMIN, Role.GUARDIAN}

PEOPLE_VIEW = ["tenants.view_person"]
PEOPLE_MANAGE = ["tenants.add_person", "tenants.change_person", "tenants.delete_person"]
OWNER_ONLY = ["tenants.change_account"]
DEVICES_VIEW = ["devices.view_device"]
DEVICES_MANAGE = ["devices.add_device", "devices.change_device", "devices.delete_device"]

_MANAGE_PEOPLE = PEOPLE_VIEW + PEOPLE_MANAGE + DEVICES_VIEW + DEVICES_MANAGE
ROLE_PERMS = {
    Role.OWNER: VIEW + OPERATE + ADMINISTER + _MANAGE_PEOPLE + OWNER_ONLY,
    Role.ADMIN: VIEW + OPERATE + ADMINISTER + _MANAGE_PEOPLE,
    Role.GUARDIAN: VIEW + OPERATE + ADMINISTER + _MANAGE_PEOPLE,
    Role.OPERATOR: VIEW + OPERATE + PEOPLE_VIEW + DEVICES_VIEW,
    Role.VIEWER: VIEW + PEOPLE_VIEW + DEVICES_VIEW,
}

# Feature names, used for permissions (below), the sidebar and URL gating.
FEATURES_BY_KIND = {
    Kind.FAMILY: {"people", "devices", "alerts", "notifications", "team", "audit"},
    Kind.SOLO: {
        "servers",
        "monitors",
        "operations",
        "api",
        "alerts",
        "notifications",
        "maintenance",
        "audit",
    },  # fmt: skip
    Kind.COMPANY: {
        "servers",
        "monitors",
        "operations",
        "api",
        "alerts",
        "notifications",
        "maintenance",
        "audit",
        "team",
        "people",
        "devices",
    },  # fmt: skip
}

# Which feature a permission belongs to: by model first, then by app.
_MODEL_FEATURE = {
    "maintenancewindow": "maintenance",
    "channel": "notifications",
    "person": "people",
    "account": None,  # always available
}
_APP_FEATURE = {
    "fleet": "servers",
    "operations": "operations",
    "monitors": "monitors",
    "users": "team",
    "audit": "audit",
    "alerts": "alerts",
    "devices": "devices",
}


def feature_of(perm: str) -> str | None:
    app_label, codename = perm.split(".", 1)
    if codename == "run_shell":
        return "operations"
    for model, feature in _MODEL_FEATURE.items():
        if codename.endswith("_" + model):
            return feature
    return _APP_FEATURE.get(app_label)


@cache
def permissions_for(kind: str, role: str) -> frozenset[str]:
    features = FEATURES_BY_KIND.get(kind, set())
    return frozenset(
        p for p in ROLE_PERMS.get(role, []) if (f := feature_of(p)) is None or f in features
    )


def has_feature(kind: str, feature: str) -> bool:
    return feature in FEATURES_BY_KIND.get(kind, set())


def roles_for(kind: str) -> list[Role]:
    return ROLES_BY_KIND.get(kind, [])
