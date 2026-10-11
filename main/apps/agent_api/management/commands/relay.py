import logging
import signal

from django.core.management.base import BaseCommand

from apps.agent_api.bridge import Bridge


class Command(BaseCommand):
    help = (
        "Run the relay bridge: receive agent heartbeats and results from Redis, retry queued "
        "jobs, detect offline servers and prune old metrics. Run exactly one."
    )

    def handle(self, *args, **options):
        logging.getLogger("apps").setLevel(logging.INFO)
        bridge = Bridge()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: bridge.stop.set())
        self.stdout.write("Relay bridge running (Ctrl-C to stop)")
        bridge.run()
