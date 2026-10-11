from django.apps import AppConfig
from django.db.models.signals import post_migrate


class UsersConfig(AppConfig):
    name = "apps.users"
    label = "users"
    verbose_name = "Users"

    def ready(self):
        from . import signals  # noqa: F401  login audit hooks
        from .roles import sync_roles

        # Groups always match ROLE_MATRIX after a migrate, including revocations.
        post_migrate.connect(lambda **kw: sync_roles(), dispatch_uid="users.sync_roles", weak=False)
