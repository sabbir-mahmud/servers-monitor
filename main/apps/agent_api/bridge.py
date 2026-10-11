"""The dashboard's side of the relay: one small process, `manage.py relay`.

- Receiver thread: BLPOPs every enabled server's inbox and applies heartbeats, acks and
  command output. One blocking call covers the whole fleet.
- Scheduler thread: re-sends commands Redis couldn't take at queue time (an outbox), runs
  the offline/expiry sweep every 15 s and prunes old metrics hourly. These are DB-only and
  cost no Redis commands while nothing is queued.
- Checker thread: application health checks (plain HTTPS from this machine, no Redis).

It replaces Celery + beat. Run exactly one per dashboard.
"""

import ipaddress
import logging
import threading
import time

import redis
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections

from apps.alerts.services import sweep
from apps.fleet.models import Server
from apps.fleet.services import ingest_report, prune_samples
from apps.monitors.checker import Checker, prune_checks
from apps.operations import services as ops
from apps.operations.models import CommandStatus

from . import relay
from .schema import PayloadError, clean_report, clean_result

logger = logging.getLogger(__name__)

STOP_AGENT_STATUSES = {CommandStatus.LOST, CommandStatus.CANCELLED, CommandStatus.EXPIRED}


def _address(report: dict) -> str | None:
    raw = ((report.get("host") or {}).get("address") or "") if isinstance(report, dict) else ""
    try:
        return str(ipaddress.ip_address(str(raw)))
    except ValueError:
        return None


def handle(server: Server, message: dict) -> None:
    """Apply one message from `server`'s agent. Never raises on bad input: one confused
    agent must not stop the receiver for the whole fleet."""
    kind = message.get("t")
    try:
        if kind == "hb":
            if not server.is_enabled:
                return
            raw = message.get("report")
            ingest_report(server, clean_report(raw), _address(raw))
            if message.get("interval") != server.report_interval:
                relay.push(server, {"t": "cfg", "report_interval": server.report_interval})
        elif kind == "ack":
            command = ops.acknowledge(server, message.get("id"))
            if command is not None and command.status in STOP_AGENT_STATUSES:
                relay.push(server, {"t": "cancel", "id": str(command.pk)})
        elif kind == "res":
            result = clean_result(message)
            command = ops.report_progress(
                server, message.get("id"), result["status"], result["output"], result["exit_code"]
            )
            # Output for a command nobody is waiting on any more: tell the agent to stop.
            if command is not None and command.status in STOP_AGENT_STATUSES:
                relay.push(server, {"t": "cancel", "id": str(command.pk)})
        else:
            logger.warning("Unknown message type %r from %s", kind, server.name)
    except PayloadError as exc:
        logger.warning("Bad %s message from %s: %s", kind, server.name, exc)
    except (ValueError, TypeError, ValidationError) as exc:  # e.g. a non-UUID command id
        logger.warning("Unusable %s message from %s: %s", kind, server.name, exc)


class Bridge:
    def __init__(self, block_seconds: int | None = None):
        self.block = block_seconds or settings.RELAY_BLOCK_SECONDS
        self.stop = threading.Event()

    # ---------------------------------------------------------------- receiver
    def receive_forever(self) -> None:
        backoff = 1
        while not self.stop.is_set():
            close_old_connections()
            ids = list(Server.objects.filter(is_enabled=True).values_list("pk", flat=True))
            try:
                got = relay.receive(ids, self.block)
                backoff = 1
            except (redis.RedisError, relay.RelayError, OSError) as exc:
                logger.warning("Relay unavailable: %s (retrying in %ss)", exc, backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if got is None:
                continue
            server_id, message = got
            server = Server.objects.filter(pk=server_id).first()
            if server is not None:
                handle(server, message)

    # ---------------------------------------------------------------- scheduler
    def schedule_forever(self) -> None:
        last_sweep = last_prune = 0.0
        while not self.stop.is_set():
            close_old_connections()
            now = time.monotonic()
            try:
                ops.dispatch_pending()
                if now - last_sweep >= settings.MONITOR_SWEEP_SECONDS:
                    last_sweep = now
                    result = sweep()
                    if any(result.values()):
                        logger.info("Sweep: %s", result)
                if now - last_prune >= 3600:
                    last_prune = now
                    if deleted := prune_samples() + prune_checks():
                        logger.info("Pruned %d samples/checks past retention", deleted)
            except Exception:  # keep scheduling even if one round hits a bug
                logger.exception("Scheduler round failed")
            relay.mark_alive()
            self.stop.wait(5)

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.receive_forever, name="relay-receive", daemon=True),
            threading.Thread(target=self.schedule_forever, name="relay-schedule", daemon=True),
            threading.Thread(target=Checker(self.stop).run_forever, name="app-checks", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while not self.stop.wait(1):
                pass
        finally:
            self.stop.set()
