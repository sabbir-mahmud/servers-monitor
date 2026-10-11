import time

from django.core.management.base import BaseCommand

from apps.alerts.services import sweep
from apps.fleet.services import prune_samples


class Command(BaseCommand):
    help = (
        "Run the offline/command-expiry sweep once (or --loop). The relay process "
        "(manage.py relay) already does this every 15 s; this is for debugging and cron."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, metavar="SECONDS", help="Repeat every N seconds")
        parser.add_argument("--prune", action="store_true", help="Also prune old metrics")

    def handle(self, *args, loop=None, prune=False, **options):
        while True:
            result = sweep()
            if prune:
                result["pruned"] = prune_samples()
            self.stdout.write(str(result))
            if not loop:
                return
            time.sleep(loop)
