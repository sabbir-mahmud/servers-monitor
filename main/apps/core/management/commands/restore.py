from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.agent_api import relay
from apps.core.backup import check_integrity, restore


class Command(BaseCommand):
    help = "Replace the database with a snapshot. Stop the web and relay processes first."

    def add_arguments(self, parser):
        parser.add_argument("snapshot", type=Path)
        parser.add_argument(
            "--force", action="store_true", help="skip the 'relay is running' check"
        )

    def handle(self, *args, snapshot: Path, force=False, **options):
        if not snapshot.is_file():
            raise CommandError(f"{snapshot} doesn't exist")
        if relay.is_alive() and not force:
            raise CommandError(
                "The relay process is running. Stop web and relay first (--force to override)."
            )
        result = check_integrity(snapshot)
        if result != "ok":
            raise CommandError(f"{snapshot} failed the integrity check: {result}")
        kept = restore(snapshot)
        self.stdout.write(f"Restored {snapshot}. The previous database was kept as {kept}.")
        self.stdout.write("Start web and relay again; remember main/.env (SECRET_KEY) must match.")
