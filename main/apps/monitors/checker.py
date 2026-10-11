"""Run application health checks and turn results into state, history and alerts.

A failed check is retried after RETRY_SECONDS instead of the full interval, and a monitor
only goes DOWN after `failure_threshold` consecutive failures: one dropped packet should
never page anyone. Recovery is immediate on the first good check.
"""

import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind, Severity

from .models import Method, Monitor, MonitorCheck, MonitorStatus

logger = logging.getLogger(__name__)

RETRY_SECONDS = 15
BODY_LIMIT = 1024 * 1024  # bytes searched for the keyword
TLS_RECHECK = timedelta(hours=12)
CERT_WARN_DAYS = 14
CERT_CRITICAL_DAYS = 3
USER_AGENT = "ServerMonitor/2 (uptime check)"


@dataclass
class Result:
    ok: bool
    status_code: int | None = None
    response_ms: int | None = None
    error: str = ""


def _context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _describe(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS certificate invalid: {reason.verify_message}"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return str(reason)[:200] or type(reason).__name__


def probe(monitor: Monitor) -> Result:
    """One HTTP(S) request. Never raises."""
    request = urllib.request.Request(
        monitor.url, method=monitor.method, headers={"User-Agent": USER_AGENT}
    )
    want_body = bool(monitor.keyword) and monitor.method == Method.GET
    started = time.perf_counter()
    code, body, error = None, b"", ""
    try:
        with urllib.request.urlopen(
            request, timeout=monitor.timeout_seconds, context=_context(monitor.verify_tls)
        ) as resp:
            code = resp.status
            if want_body:
                body = resp.read(BODY_LIMIT)
    except urllib.error.HTTPError as exc:  # 4xx/5xx still answer: the status decides
        code = exc.code
        if want_body:
            try:
                body = exc.read(BODY_LIMIT)
            except OSError:
                body = b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        error = _describe(exc)
    elapsed = int((time.perf_counter() - started) * 1000)
    if code is None:
        return Result(ok=False, error=error)
    if not any(lo <= code <= hi for lo, hi in monitor.expected_ranges):
        return Result(ok=False, status_code=code, response_ms=elapsed, error=f"HTTP {code}")
    if want_body and monitor.keyword.encode() not in body:
        return Result(
            ok=False,
            status_code=code,
            response_ms=elapsed,
            error=f"keyword {monitor.keyword!r} not found",
        )
    return Result(ok=True, status_code=code, response_ms=elapsed)


def cert_expiry(monitor: Monitor) -> datetime | None:
    """Expiry of a valid certificate. An invalid or expired one returns None here and is
    reported by the check itself ("TLS certificate invalid: certificate has expired")."""
    parts = urlsplit(monitor.url)
    if parts.scheme != "https" or not parts.hostname or not monitor.verify_tls:
        return None
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=10) as raw:
            with _context(True).wrap_socket(raw, server_hostname=parts.hostname) as tls:
                not_after = (tls.getpeercert() or {}).get("notAfter")
    except OSError, ssl.SSLError:
        return None
    if not not_after:
        return None
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)


# --------------------------------------------------------------------------- alerts


def _notify(alert: Alert, event: str) -> None:
    from apps.alerts.notify import notify

    transaction.on_commit(lambda: notify(str(alert.pk), event))


def _open_alert(monitor, kind, severity, message) -> None:
    existing = Alert.objects.filter(monitor=monitor, kind=kind, resolved_at=None).first()
    if existing is not None:
        if existing.severity != severity or existing.message != message[:300]:
            existing.severity, existing.message = severity, message[:300]
            existing.save(update_fields=["severity", "message"])
        return
    try:
        with transaction.atomic():
            alert = Alert.objects.create(
                monitor=monitor, kind=kind, severity=severity, message=message[:300]
            )
    except IntegrityError:  # opened concurrently by a "check now"
        return
    _notify(alert, "opened")


def _resolve_alert(monitor, kind) -> None:
    for alert in Alert.objects.filter(monitor=monitor, kind=kind, resolved_at=None):
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["resolved_at"])
        _notify(alert, "resolved")


def resolve_all(monitor: Monitor) -> None:
    for kind in (AlertKind.APP_DOWN, AlertKind.CERT):
        _resolve_alert(monitor, kind)


# --------------------------------------------------------------------------- state


def record(monitor: Monitor, result: Result, now=None) -> Monitor:
    """Store one result and move the monitor's state machine. Returns the fresh monitor."""
    now = now or timezone.now()
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor.pk)
        MonitorCheck.objects.create(
            monitor=monitor,
            checked_at=now,
            ok=result.ok,
            status_code=result.status_code,
            response_ms=result.response_ms,
            error=result.error[:300],
        )
        monitor.last_checked_at = now
        monitor.last_status_code = result.status_code
        monitor.last_response_ms = result.response_ms
        monitor.last_error = result.error[:300]
        previous = monitor.status
        if result.ok:
            monitor.consecutive_failures = 0
            monitor.status = MonitorStatus.UP
            monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)
        else:
            monitor.consecutive_failures += 1
            if monitor.consecutive_failures >= monitor.failure_threshold:
                monitor.status = MonitorStatus.DOWN
                monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)
            else:
                # Not down yet: confirm quickly instead of waiting a whole interval.
                monitor.next_check_at = now + timedelta(
                    seconds=min(RETRY_SECONDS, monitor.interval_seconds)
                )
        if monitor.status != previous:
            monitor.status_since = now
        monitor.save()

        if monitor.status == MonitorStatus.DOWN and previous != MonitorStatus.DOWN:
            _open_alert(
                monitor,
                AlertKind.APP_DOWN,
                Severity.CRITICAL,
                f"{monitor.name} is down: {result.error or 'check failed'}",
            )
        elif monitor.status == MonitorStatus.UP and previous == MonitorStatus.DOWN:
            _resolve_alert(monitor, AlertKind.APP_DOWN)
    return monitor


def update_certificate(monitor: Monitor, now=None) -> None:
    now = now or timezone.now()
    if monitor.tls_checked_at and now - monitor.tls_checked_at < TLS_RECHECK:
        return
    expires = cert_expiry(monitor)
    Monitor.objects.filter(pk=monitor.pk).update(tls_expires_at=expires, tls_checked_at=now)
    if expires is None:
        return
    days = (expires - now).days
    with transaction.atomic():
        if days < CERT_WARN_DAYS:
            severity = Severity.CRITICAL if days < CERT_CRITICAL_DAYS else Severity.WARNING
            when = "has expired" if days < 0 else f"expires in {days} day{'s' if days != 1 else ''}"
            _open_alert(
                monitor, AlertKind.CERT, severity, f"TLS certificate for {monitor.name} {when}"
            )
        else:
            _resolve_alert(monitor, AlertKind.CERT)


def check(monitor: Monitor) -> Monitor:
    """Probe, record, and refresh the certificate date when due."""
    monitor = record(monitor, probe(monitor))
    update_certificate(monitor)
    return monitor


def prune_checks(batch_size: int = 5000) -> int:
    cutoff, total = MonitorCheck.retention_cutoff(), 0
    while True:
        ids = list(
            MonitorCheck.objects.filter(checked_at__lt=cutoff).values_list("pk", flat=True)[
                :batch_size
            ]
        )
        if not ids:
            return total
        total += MonitorCheck.objects.filter(pk__in=ids).delete()[0]


# --------------------------------------------------------------------------- scheduler


class Checker:
    """Runs due checks on a small thread pool. Lives in the relay process."""

    def __init__(self, stop: threading.Event, workers: int | None = None):
        self.stop = stop
        self.pool = ThreadPoolExecutor(
            max_workers=workers or settings.MONITOR_CHECK_WORKERS, thread_name_prefix="check"
        )
        self.in_flight: set = set()
        self.lock = threading.Lock()

    def _run(self, monitor: Monitor) -> None:
        close_old_connections()
        try:
            check(monitor)
        except Exception:  # one broken monitor must not stop the others
            logger.exception("Check for %s crashed", monitor.name)
        finally:
            with self.lock:
                self.in_flight.discard(monitor.pk)
            close_old_connections()

    def tick(self) -> int:
        now = timezone.now()
        due = Monitor.objects.filter(is_enabled=True, next_check_at__lte=now).order_by(
            "next_check_at"
        )[:50]
        started = 0
        for monitor in due:
            with self.lock:
                if monitor.pk in self.in_flight:
                    continue
                self.in_flight.add(monitor.pk)
            # Push the next run out now so a slow check is never started twice.
            Monitor.objects.filter(pk=monitor.pk).update(
                next_check_at=now + timedelta(seconds=monitor.interval_seconds)
            )
            self.pool.submit(self._run, monitor)
            started += 1
        return started

    def run_forever(self) -> None:
        while not self.stop.is_set():
            close_old_connections()
            try:
                self.tick()
            except Exception:
                logger.exception("Checker tick failed")
            self.stop.wait(1)
        self.pool.shutdown(wait=False, cancel_futures=True)
