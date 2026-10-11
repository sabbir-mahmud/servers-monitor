"""The dashboard's side of the relay: one small process, `manage.py relay`.

- Receiver thread: BLPOPs every enabled server's inbox and applies heartbeats, acks and
  command output. One blocking call covers the whole fleet.
- Scheduler thread: re-sends commands Redis couldn't take at queue time (an outbox), runs
  the offline/expiry sweep every 15 s and prunes old metrics hourly. These are DB-only and
  cost no Redis commands while nothing is queued.
- Checker thread: application health checks (plain HTTPS from this machine, no Redis).
- Notify thread: sends alert notifications from the database outbox (Telegram, webhooks).

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
from django.utils import timezone

from apps.alerts import delivery, forecast
from apps.alerts.services import sweep
from apps.core import backup
from apps.fleet import rollups
from apps.fleet.models import Server
from apps.fleet.services import ingest_report, prune_samples
from apps.monitors.checker import Checker, prune_checks
from apps.operations import services as ops
from apps.operations.models import CommandStatus
from apps.status import publish as status_publish

from . import relay, signing, usage
from .schema import PayloadError, clean_report, clean_result

logger = logging.getLogger(__name__)

STOP_AGENT_STATUSES = {CommandStatus.LOST, CommandStatus.CANCELLED, CommandStatus.EXPIRED}


def _address(report: dict) -> str | None:
    raw = ((report.get("host") or {}).get("address") or "") if isinstance(report, dict) else ""
    try:
        return str(ipaddress.ip_address(str(raw)))
    except ValueError:
        return None


class RateLimiter:
    """Per-server token bucket: a burst of `capacity` messages, refilled at `rate`/s.
    Enough for streamed output from several commands; a flooding agent just loses the
    excess instead of filling the database (and your Redis bill)."""

    def __init__(self, capacity: int = 120, rate: float = 2.0):
        self.capacity, self.rate = capacity, rate
        self.buckets: dict[str, tuple[float, float]] = {}
        self.warned: dict[str, float] = {}

    def allow(self, server_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self.buckets.get(server_id, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[server_id] = (tokens, now)
            if now - self.warned.get(server_id, -1e9) > 60:
                self.warned[server_id] = now
                logger.warning("Server %s is over its message budget; dropping messages", server_id)
            return False
        self.buckets[server_id] = (tokens - 1, now)
        return True


def _heartbeat_too_soon(server: Server) -> bool:
    """At most one stored heartbeat per half interval (5 s floor), however fast it sends."""
    if server.last_seen_at is None:
        return False
    gap = (timezone.now() - server.last_seen_at).total_seconds()
    return gap < max(5, server.report_interval / 2)


def handle(server: Server, message: dict) -> None:
    """Apply one message from `server`'s agent. Never raises on bad input: one confused
    agent must not stop the receiver for the whole fleet."""
    kind = message.get("t")
    usage.count_agent_message(kind)
    if not signing.verify_from_server(server, message):
        # Forged (written by someone with Redis access but not this server's key) or
        # corrupted. Either way: not from the agent, so it changes nothing.
        logger.warning("Rejected unsigned or forged %r message for %s", kind, server.name)
        return
    try:
        if kind == "hb":
            if not server.is_enabled or _heartbeat_too_soon(server):
                return
            raw = message.get("report")
            ingest_report(server, clean_report(raw), _address(raw))
            cfg = _probe_config(server, raw) or {}
            if cfg or message.get("interval") != server.report_interval:
                relay.push(server, {"t": "cfg", "report_interval": server.report_interval, **cfg})
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
        elif kind == "ping":
            _job_ping(server, message)
        else:
            logger.warning("Unknown message type %r from %s", kind, server.name)
    except PayloadError as exc:
        logger.warning("Bad %s message from %s: %s", kind, server.name, exc)
    except (ValueError, TypeError, ValidationError) as exc:  # e.g. a non-UUID command id
        logger.warning("Unusable %s message from %s: %s", kind, server.name, exc)


def _probe_config(server: Server, raw: dict) -> dict | None:
    """Agents as uptime probes (3.4): store the results riding on this heartbeat, and
    return new assignments if the agent's copy is out of date (it echoes their revision,
    so assignments are only re-sent when they change or the agent restarted)."""
    from apps.monitors import checker

    features = server.capabilities.get("features") or []
    if "probes" not in features or not server.signing_salt or not isinstance(raw, dict):
        return None  # older or legacy unsigned agents never get probes
    checker.receive_probe_results(server, raw.get("probe_results"))
    assignments = checker.probe_assignments(server)
    rev = checker.assignments_rev(assignments) if assignments else ""
    if str(raw.get("probes_rev") or "") == rev:
        return None
    return {"probes": assignments, "probes_rev": rev}


def _job_ping(server: Server, message: dict) -> None:
    """`smctl ping`/`smctl run` on `server`: feed its Heartbeat monitor with that name.
    Only signed messages count (a legacy unsigned agent can't vouch for its pings), and
    a server can only ping its own heartbeats."""
    from apps.monitors.checker import receive_ping
    from apps.monitors.models import Kind, Monitor

    if not server.signing_salt:
        logger.warning("Ignoring job ping from %s: legacy unsigned agent", server.name)
        return
    name, status = message.get("name"), message.get("status")
    if not isinstance(name, str) or status not in ("ok", "fail", "start"):
        raise PayloadError("ping needs a name and a status of ok, fail or start")
    exit_code = message.get("exit")
    if exit_code is not None and not isinstance(exit_code, int):
        exit_code = None
    monitor = Monitor.objects.filter(
        kind=Kind.HEARTBEAT, server=server, target=name[:253], is_enabled=True
    ).first()
    if monitor is None:
        logger.info("Job ping %r from %s matches no enabled Heartbeat monitor", name, server.name)
        return
    receive_ping(monitor, status, exit_code)


class Bridge:
    def __init__(self, block_seconds: int | None = None):
        self.block = block_seconds or settings.RELAY_BLOCK_SECONDS
        self.stop = threading.Event()
        self.limiter = RateLimiter()

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
            if not self.limiter.allow(server_id):
                continue
            server = Server.objects.filter(pk=server_id).first()
            if server is not None:
                handle(server, message)

    # ---------------------------------------------------------------- scheduler
    def schedule_forever(self) -> None:
        last_sweep = last_prune = last_usage = last_status = 0.0
        while not self.stop.is_set():
            close_old_connections()
            now = time.monotonic()
            try:
                ops.dispatch_pending()
                ops.run_due_schedules()
                ops.advance_rollouts()
                if now - last_sweep >= settings.MONITOR_SWEEP_SECONDS:
                    last_sweep = now
                    result = sweep()
                    if any(result.values()):
                        logger.info("Sweep: %s", result)
                if backup.snapshot_due():
                    backup.take_snapshot()
                if now - last_status >= 120 or status_publish.requested():
                    last_status = now
                    status_publish.publish_due()
                if now - last_usage >= 60:
                    last_usage = now
                    usage.flush()
                if now - last_prune >= 3600:
                    last_prune = now
                    usage.prune()
                    rollups.build_all()  # before the forecast, which reads them
                    forecast.update_all()
                    if deleted := prune_samples() + prune_checks():
                        logger.info("Pruned %d samples/checks past retention", deleted)
            except Exception:  # keep scheduling even if one round hits a bug
                logger.exception("Scheduler round failed")
            relay.mark_alive()
            self.stop.wait(5)

    def notify_forever(self) -> None:
        """Alert notifications (Telegram, webhooks): outbound HTTPS, no Redis."""
        last_prune = 0.0
        while not self.stop.is_set():
            close_old_connections()
            try:
                delivery.process()
                if time.monotonic() - last_prune >= 3600:
                    last_prune = time.monotonic()
                    delivery.prune()
            except Exception:
                logger.exception("Notification round failed")
            self.stop.wait(5)

    def watchdog_forever(self) -> None:
        """Ping WATCHDOG_URL every minute (see apps/core/system.py)."""
        from apps.core.system import WATCHDOG_EVERY, ping_watchdog

        while not self.stop.is_set():
            close_old_connections()
            try:
                ping_watchdog()
            except Exception:
                logger.exception("Watchdog round failed")
            self.stop.wait(WATCHDOG_EVERY)

    def run(self) -> None:
        # Before marking ourselves alive again: was monitoring down, and for how long?
        from apps.core.system import check_downtime

        try:
            check_downtime(relay.last_alive_at())
        except Exception:
            logger.exception("Downtime check failed")
        relay.mark_alive()
        threads = [
            threading.Thread(target=self.watchdog_forever, name="watchdog", daemon=True),
            threading.Thread(target=self.notify_forever, name="notify", daemon=True),
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
