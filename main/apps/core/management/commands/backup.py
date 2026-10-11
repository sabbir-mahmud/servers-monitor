from django.core.management.base import BaseCommand, CommandError

from apps.core.backup import take_snapshot


class Command(BaseCommand):
    help = "Take a consistent snapshot of the database now (the relay also does this daily)."

    def handle(self, *args, **options):
        state = take_snapshot()
        if not state["ok"]:
            raise CommandError(f"Snapshot failed: {state['error']}")
        self.stdout.write(
            f"Snapshot written to {state['path']} ({state['size']:,} bytes); {state['count']} kept."
        )
