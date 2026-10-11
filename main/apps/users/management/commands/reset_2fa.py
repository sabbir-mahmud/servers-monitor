from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.audit.services import record
from apps.users.twofactor import disable


class Command(BaseCommand):
    help = "Remove a user's two-factor device and recovery codes (break-glass, run on the host)."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, username, **options):
        user = get_user_model().objects.filter(username__iexact=username).first()
        if user is None:
            raise CommandError(f"No user {username!r}")
        disable(user)
        record(None, "user.2fa_reset", target=user.get_username(), via="manage.py")
        self.stdout.write(
            f"Two-factor removed for {user.get_username()}. They set it up again at next sign-in."
        )
