from django.core.management.base import BaseCommand

from apps.users.roles import ROLE_MATRIX, sync_roles


class Command(BaseCommand):
    help = "Make the Viewer/Operator/Admin groups match ROLE_MATRIX (adds and revokes)."

    def handle(self, *args, **options):
        sync_roles()
        self.stdout.write(self.style.SUCCESS(f"Synced roles: {', '.join(ROLE_MATRIX)}"))
