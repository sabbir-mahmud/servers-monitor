"""Run application health checks and turn results into state, history and alerts.

A failed check is retried after RETRY_SECONDS instead of the full interval, and a monitor
only goes DOWN after `failure_threshold` consecutive failures: one dropped packet should
never page anyone. Recovery is immediate on the first good check.
"""

import ipaddress
import json
import logging
import platform
import re
import socket
import ssl
import subprocess
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
from apps.core import netguard

from .models import Kind, Method, Monitor, MonitorCheck, MonitorStatus

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
    degraded: bool = False
    expires_at: datetime | None = None  # domain registration expiry


def _slow(monitor: Monitor, result: Result) -> Result:
    """Mark a successful but slower-than-allowed result as degraded (amber, not down)."""
    limit = monitor.opt("slow_ms")
    if result.ok and limit and result.response_ms is not None and result.response_ms > limit:
        result.degraded = True
        result.error = f"slow: {result.response_ms} ms (limit {limit} ms)"
    return result


def _context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _describe(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, netguard.BlockedAddress):
        return str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS certificate invalid: {reason.verify_message}"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return str(reason)[:200] or type(reason).__name__


def probe_http(monitor: Monitor) -> Result:
    """One HTTP(S) request. Never raises."""
    body_out = (
        monitor.opt("body", "").encode() if monitor.method in (Method.POST, Method.PUT) else None
    )
    headers = {"User-Agent": USER_AGENT, **monitor.headers}
    request = urllib.request.Request(
        monitor.url, data=body_out, method=monitor.method, headers=headers
    )
    want_body = bool(monitor.keyword) and monitor.method != Method.HEAD
    started = time.perf_counter()
    code, body, error = None, b"", ""
    open_kwargs = {"timeout": monitor.timeout_seconds, "context": _context(monitor.verify_tls)}
    if monitor.opt("follow_redirects", True) is False:
        open_kwargs["follow_redirects"] = False
    try:
        with netguard.urlopen(request, **open_kwargs) as resp:
            code = resp.status
            if want_body:
                body = resp.read(BODY_LIMIT)
    except urllib.error.HTTPError as exc:  # 4xx/5xx (and unfollowed 3xx) still answer
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
    if want_body:
        found = monitor.keyword.encode() in body
        if monitor.opt("keyword_absent") and found:
            return Result(ok=False, status_code=code, response_ms=elapsed,
                          error=f"keyword {monitor.keyword!r} appeared")  # fmt: skip
        if not monitor.opt("keyword_absent") and not found:
            return Result(ok=False, status_code=code, response_ms=elapsed,
                          error=f"keyword {monitor.keyword!r} not found")  # fmt: skip
    return _slow(monitor, Result(ok=True, status_code=code, response_ms=elapsed))


def probe_tcp(monitor: Monitor) -> Result:
    """Connect (and optionally complete a TLS handshake). Never raises."""
    host, port = monitor.target, int(monitor.opt("port") or 0)
    started = time.perf_counter()
    try:
        # Check the address before connecting: otherwise "refused" vs "blocked" would map
        # which internal ports are open.
        ip = _resolve_allowed(host)
        with netguard.create_connection((ip, port), timeout=monitor.timeout_seconds) as sock:
            if monitor.opt("tls"):
                with _context(monitor.verify_tls).wrap_socket(sock, server_hostname=host):
                    pass
    except (OSError, ssl.SSLError) as exc:
        return Result(ok=False, error=_describe(exc))
    return _slow(monitor, Result(ok=True, response_ms=int((time.perf_counter() - started) * 1000)))


PING_LOSS = re.compile(r"([\d.]+)% packet loss")
PING_RTT = re.compile(r"= [\d.]+/([\d.]+)/")


def _resolve_allowed(host: str) -> str:
    """The address we'll actually contact, refused if it's internal (no DNS rebinding:
    the checked IP is the one used)."""
    try:
        ip = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        ip = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0]
    if not netguard.is_allowed(ip):
        raise netguard.BlockedAddress(netguard.describe_block(ip))
    return ip


def probe_ping(monitor: Monitor) -> Result:
    """Three ICMP echoes with the system `ping` (needs NET_RAW in Docker). Never raises."""
    try:
        ip = _resolve_allowed(monitor.target)
    except OSError as exc:
        return Result(ok=False, error=_describe(exc))
    wait = str(monitor.timeout_seconds)
    binary = "ping6" if ":" in ip and platform.system() == "Darwin" else "ping"
    cmd = [binary, "-n", "-c", "3"] + (
        ["-t", wait] if platform.system() == "Darwin" else ["-w", wait]
    )
    try:
        out = subprocess.run(cmd + [ip], capture_output=True, text=True,
                             timeout=monitor.timeout_seconds + 5).stdout  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(ok=False, error=f"ping unavailable: {exc}"[:200])
    loss = PING_LOSS.search(out)
    rtt = PING_RTT.search(out)
    if not loss or float(loss.group(1)) >= 100:
        return Result(ok=False, error="no reply")
    ms = int(float(rtt.group(1))) if rtt else None
    result = Result(ok=True, response_ms=ms)
    if float(loss.group(1)) > 0:
        result.degraded, result.error = True, f"{float(loss.group(1)):.0f}% packet loss"
        return result
    return _slow(monitor, result)


def probe_dns(monitor: Monitor) -> Result:
    """Resolve a record; optionally require an answer containing `expected`. Never raises."""
    import dns.exception
    import dns.resolver

    record_type = monitor.opt("record_type", "A")
    resolver = dns.resolver.Resolver(configure=not monitor.opt("resolver"))
    if monitor.opt("resolver"):
        resolver.nameservers = [monitor.opt("resolver")]
    resolver.lifetime = monitor.timeout_seconds
    started = time.perf_counter()
    try:
        answer = resolver.resolve(monitor.target, record_type)
    except dns.resolver.NXDOMAIN:
        return Result(ok=False, error="domain does not exist (NXDOMAIN)")
    except dns.resolver.NoAnswer:
        return Result(ok=False, error=f"no {record_type} record")
    except (dns.exception.DNSException, OSError, ValueError) as exc:
        return Result(ok=False, error=f"DNS error: {exc}"[:200])
    elapsed = int((time.perf_counter() - started) * 1000)
    values = sorted(r.to_text().strip('"') for r in answer)
    expected = (monitor.opt("expected") or "").strip()
    if expected and not any(expected.lower() in v.lower() for v in values):
        return Result(ok=False, response_ms=elapsed,
                      error=f"expected {expected!r}, got {', '.join(values)}"[:300])  # fmt: skip
    return _slow(monitor, Result(ok=True, response_ms=elapsed))


RDAP_URL = "https://rdap.org/domain/{}"


def probe_domain(monitor: Monitor) -> Result:
    """Registration expiry from RDAP (the JSON successor to WHOIS). Never raises."""
    request = urllib.request.Request(
        RDAP_URL.format(monitor.target.rstrip(".").lower()),
        headers={"User-Agent": USER_AGENT, "Accept": "application/rdap+json"},
    )
    try:
        with netguard.urlopen(request, timeout=monitor.timeout_seconds + 10) as resp:
            data = json.loads(resp.read(BODY_LIMIT))
    except urllib.error.HTTPError as exc:
        return Result(ok=False, status_code=exc.code,
                      error="domain not found in RDAP" if exc.code == 404 else f"RDAP HTTP {exc.code}")  # fmt: skip
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Result(ok=False, error=f"RDAP: {_describe(exc)}")
    expires = next(
        (
            e.get("eventDate")
            for e in data.get("events", [])
            if e.get("eventAction") == "expiration"
        ),
        None,
    )
    if not expires:
        return Result(ok=False, error="registry doesn't publish an expiry date")
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return Result(ok=False, error=f"unreadable expiry {expires!r}")
    if when <= timezone.now():
        return Result(ok=False, expires_at=when, error="domain registration has expired")
    return Result(ok=True, expires_at=when)


PROBES = {
    Kind.HTTP: probe_http,
    Kind.TCP: probe_tcp,
    Kind.PING: probe_ping,
    Kind.DNS: probe_dns,
    Kind.DOMAIN: probe_domain,
}


def probe(monitor: Monitor) -> Result:
    return PROBES[monitor.kind](monitor)


def cert_expiry(monitor: Monitor) -> datetime | None:
    """Expiry of a valid certificate. An invalid or expired one returns None here and is
    reported by the check itself ("TLS certificate invalid: certificate has expired")."""
    if not monitor.verify_tls:
        return None
    if monitor.kind == Kind.HTTP:
        parts = urlsplit(monitor.url)
        if parts.scheme != "https" or not parts.hostname:
            return None
        host, port = parts.hostname, parts.port or 443
    elif monitor.kind == Kind.TCP and monitor.opt("tls"):
        host, port = monitor.target, int(monitor.opt("port") or 0)
    else:
        return None
    try:
        with netguard.create_connection((host, port), timeout=10) as raw:
            with _context(True).wrap_socket(raw, server_hostname=host) as tls:
                not_after = (tls.getpeercert() or {}).get("notAfter")
    except OSError, ssl.SSLError:
        return None
    if not not_after:
        return None
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)


# --------------------------------------------------------------------------- alerts


def _open_alert(monitor, kind, severity, message) -> None:
    existing = Alert.objects.filter(monitor=monitor, kind=kind, resolved_at=None).first()
    if existing is not None:
        if existing.severity != severity or existing.message != message[:300]:
            existing.severity, existing.message = severity, message[:300]
            existing.save(update_fields=["severity", "message"])
        return
    try:
        with transaction.atomic():
            Alert.objects.create(
                monitor=monitor, kind=kind, severity=severity, message=message[:300]
            )  # announced by the relay's notification outbox
    except IntegrityError:  # opened concurrently by a "check now"
        return


def _resolve_alert(monitor, kind) -> None:
    for alert in Alert.objects.filter(monitor=monitor, kind=kind, resolved_at=None):
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["resolved_at"])


def resolve_all(monitor: Monitor) -> None:
    for kind in (AlertKind.APP_DOWN, AlertKind.CERT, AlertKind.DOMAIN):
        _resolve_alert(monitor, kind)


# --------------------------------------------------------------------------- state


DASHBOARD = "dashboard"


def _stale_after(monitor: Monitor) -> int:
    """A location that hasn't reported for this long (an agent went offline, a server was
    removed) no longer counts towards the quorum."""
    return max(3 * monitor.interval_seconds, 600)


def quorum_outcome(monitor: Monitor, states: dict, now) -> tuple[str, str]:
    """(status, error) from the latest result of every live location. A location counts as
    down after `failure_threshold` consecutive failures; the monitor is down when at least
    `quorum` of them are (or all of them, if fewer than `quorum` are reporting)."""
    allowed = {DASHBOARD} if monitor.check_from_dashboard else set()
    allowed |= {str(pk) for pk in monitor.probe_servers.values_list("pk", flat=True)}
    fresh = {
        loc: st
        for loc, st in states.items()
        if loc in allowed
        and (now - datetime.fromisoformat(st["at"])).total_seconds() <= _stale_after(monitor)
    }
    if not fresh:
        return MonitorStatus.PENDING, ""
    down = sorted(loc for loc, st in fresh.items()
                  if not st["ok"] and st["failures"] >= monitor.failure_threshold)  # fmt: skip
    need = max(1, min(monitor.quorum, len(fresh)))
    names = location_names(down)
    if len(down) >= need:
        first = fresh[down[0]].get("error") or "check failed"
        return MonitorStatus.DOWN, f"down from {', '.join(names)}: {first}"[:300]
    if down:
        return (
            MonitorStatus.UP,
            f"failing from {', '.join(names)} only ({len(down)} of {len(fresh)})",
        )
    if any(st.get("degraded") for st in fresh.values()):
        return MonitorStatus.DEGRADED, next(
            st["error"] for st in fresh.values() if st.get("degraded")
        )
    return MonitorStatus.UP, ""


def location_names(locations) -> list[str]:
    from apps.fleet.models import Server

    ids = [loc for loc in locations if loc != DASHBOARD]
    names = dict(Server.objects.filter(pk__in=ids).values_list("pk", "name")) if ids else {}
    names = {str(k): v for k, v in names.items()}
    return [
        "dashboard" if loc == DASHBOARD else names.get(loc, "removed server") for loc in locations
    ]


def _is_multi(monitor: Monitor, location: str) -> bool:
    return bool(location) or (monitor.supports_locations and monitor.probe_servers.exists())


def record(monitor: Monitor, result: Result, now=None, next_at=None, location: str = "") -> Monitor:
    """Store one result and move the monitor's state machine. Returns the fresh monitor.
    `next_at` overrides when it's next due (heartbeats: the next ping's deadline).
    `location` is the server id of an agent that ran the check ("" = this dashboard)."""
    now = now or timezone.now()
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor.pk)
        previous = monitor.status
        if _is_multi(monitor, location):
            # The location's own result, then the quorum outcome as the monitor's row
            # (location ""), which is what uptime, bars and incidents are computed from.
            _store_check(monitor, result, now, location or DASHBOARD)
            _apply_location(monitor, result, now, location or DASHBOARD)
            outcome = Result(
                ok=monitor.status != MonitorStatus.DOWN,
                degraded=monitor.status == MonitorStatus.DEGRADED,
                status_code=result.status_code,
                response_ms=result.response_ms,
                error=monitor.last_error,
            )
            _store_check(monitor, outcome, now, "")
        else:
            _store_check(monitor, result, now, "")
            _apply_single(monitor, result, now)
        if next_at is not None:
            monitor.next_check_at = next_at
        if monitor.status != previous:
            monitor.status_since = now
        monitor.save()

        if monitor.status == MonitorStatus.DOWN and previous != MonitorStatus.DOWN:
            _open_alert(
                monitor,
                AlertKind.APP_DOWN,
                Severity.CRITICAL,
                f"{monitor.name} is down: {monitor.last_error or result.error or 'check failed'}",
            )
        elif monitor.status != MonitorStatus.DOWN and previous == MonitorStatus.DOWN:
            _resolve_alert(monitor, AlertKind.APP_DOWN)
    return monitor


def _store_check(monitor: Monitor, result: Result, now, location: str) -> None:
    MonitorCheck.objects.create(
        monitor=monitor,
        checked_at=now,
        ok=result.ok,
        degraded=result.degraded,
        status_code=result.status_code,
        response_ms=result.response_ms,
        error=result.error[:300],
        location=location,
    )


def _apply_single(monitor: Monitor, result: Result, now) -> None:
    monitor.last_checked_at = now
    monitor.last_status_code = result.status_code
    monitor.last_response_ms = result.response_ms
    monitor.last_error = result.error[:300]
    if result.ok:
        monitor.consecutive_failures = 0
        monitor.status = MonitorStatus.DEGRADED if result.degraded else MonitorStatus.UP
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


def _apply_location(monitor: Monitor, result: Result, now, location: str) -> None:
    states = dict(monitor.location_states or {})
    failures = 0 if result.ok else int(states.get(location, {}).get("failures", 0)) + 1
    states[location] = {
        "ok": result.ok, "degraded": result.degraded, "failures": failures, "at": now.isoformat(),
        "error": result.error[:200], "ms": result.response_ms, "code": result.status_code,
    }  # fmt: skip
    monitor.location_states = states
    monitor.status, error = quorum_outcome(monitor, states, now)
    monitor.last_checked_at = now
    monitor.last_error = error
    if location == DASHBOARD:
        monitor.last_status_code = result.status_code
        monitor.last_response_ms = result.response_ms
        monitor.consecutive_failures = failures
        retry = 0 < failures < monitor.failure_threshold
        monitor.next_check_at = now + timedelta(
            seconds=min(RETRY_SECONDS, monitor.interval_seconds)
            if retry
            else monitor.interval_seconds
        )
    elif monitor.last_response_ms is None:
        monitor.last_response_ms = result.response_ms


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


DOMAIN_WARN_DAYS = 30
DOMAIN_CRITICAL_DAYS = 7


def update_domain(monitor: Monitor, result: Result, now=None) -> None:
    now = now or timezone.now()
    if result.expires_at is None:
        return
    Monitor.objects.filter(pk=monitor.pk).update(domain_expires_at=result.expires_at)
    days = (result.expires_at - now).days
    with transaction.atomic():
        if 0 <= days < DOMAIN_WARN_DAYS:
            severity = Severity.CRITICAL if days < DOMAIN_CRITICAL_DAYS else Severity.WARNING
            _open_alert(monitor, AlertKind.DOMAIN, severity,
                        f"Domain {monitor.target} expires in {days} day{'s' if days != 1 else ''}")  # fmt: skip
        else:
            _resolve_alert(monitor, AlertKind.DOMAIN)  # renewed (or expired: the check is down)


def check(monitor: Monitor) -> Monitor:
    """Probe, record, and refresh certificate/domain dates when due."""
    if monitor.kind == Kind.HEARTBEAT:
        return missed_heartbeat(monitor)
    if monitor.supports_locations and not monitor.check_from_dashboard:
        # Agents check it; just re-evaluate so silent locations age out of the quorum.
        return reevaluate(monitor)
    result = probe(monitor)
    monitor = record(monitor, result)
    if monitor.kind == Kind.DOMAIN:
        update_domain(monitor, result)
    else:
        update_certificate(monitor)
    return monitor


# --------------------------------------------------------------------------- heartbeats


def heartbeat_deadline(monitor: Monitor, now) -> datetime:
    return now + timedelta(
        seconds=int(monitor.opt("period_seconds", 86400)) + int(monitor.opt("grace_seconds", 3600))
    )


def missed_heartbeat(monitor: Monitor, now=None) -> Monitor:
    """The deadline passed with no ping: down, and look again after another period."""
    now = now or timezone.now()
    last = monitor.last_checked_at
    since = f"since {timezone.localtime(last):%b %d %H:%M}" if last else "yet"
    return record(
        monitor, Result(ok=False, error=f"no ping {since}"), now, heartbeat_deadline(monitor, now)
    )


def receive_ping(monitor: Monitor, status: str, exit_code=None, now=None) -> Monitor:
    """A ping from `smctl ping`/`smctl run`: "start", "ok" or "fail"."""
    now = now or timezone.now()
    if status == "start":
        Monitor.objects.filter(pk=monitor.pk).update(
            run_started_at=now, next_check_at=heartbeat_deadline(monitor, now)
        )
        return monitor
    runtime = None
    if monitor.run_started_at:
        runtime = int((now - monitor.run_started_at).total_seconds() * 1000)
        Monitor.objects.filter(pk=monitor.pk).update(run_started_at=None)
    if status == "ok":
        result = Result(ok=True, response_ms=runtime)
    else:
        code = f" (exit code {exit_code})" if exit_code not in (None, "") else ""
        result = Result(ok=False, response_ms=runtime, error=f"job reported failure{code}")
    # A failure the job itself reports is down at once: no retry would change it.
    if not result.ok:
        Monitor.objects.filter(pk=monitor.pk).update(
            consecutive_failures=max(monitor.failure_threshold - 1, 0)
        )
    return record(monitor, result, now, heartbeat_deadline(monitor, now))


def reevaluate(monitor: Monitor, now=None) -> Monitor:
    """Recompute an agent-only monitor's status without a new result."""
    now = now or timezone.now()
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor.pk)
        previous = monitor.status
        monitor.status, monitor.last_error = quorum_outcome(monitor, monitor.location_states, now)
        if monitor.status == MonitorStatus.PENDING and previous != MonitorStatus.PENDING:
            monitor.last_error = "no location has reported recently"
            monitor.status = previous  # keep the last known state rather than flapping
        monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)
        if monitor.status != previous:
            monitor.status_since = now
        monitor.save()
        if monitor.status == MonitorStatus.DOWN and previous != MonitorStatus.DOWN:
            _open_alert(monitor, AlertKind.APP_DOWN, Severity.CRITICAL,
                        f"{monitor.name} is down: {monitor.last_error}")  # fmt: skip
        elif monitor.status != MonitorStatus.DOWN and previous == MonitorStatus.DOWN:
            _resolve_alert(monitor, AlertKind.APP_DOWN)
    return monitor


# --------------------------------------------------------------------------- agent probes

PROBE_KINDS = (Kind.HTTP, Kind.TCP)
MAX_PROBES_PER_AGENT = 20


def probe_assignments(server) -> list[dict]:
    """What `server`'s agent should check. Never includes secret request headers: relay
    messages are signed but readable by whoever runs Redis (the form refuses that combo)."""
    out = []
    monitors = Monitor.objects.filter(
        probe_servers=server, is_enabled=True, kind__in=PROBE_KINDS
    ).order_by("pk")[:MAX_PROBES_PER_AGENT]
    for m in monitors:
        spec = {"id": str(m.pk), "kind": m.kind, "interval": m.interval_seconds,
                "timeout": m.timeout_seconds, "verify_tls": m.verify_tls}  # fmt: skip
        if m.kind == Kind.HTTP:
            spec.update(url=m.url, method=m.method, expected=m.expected_status,
                        keyword=m.keyword, keyword_absent=bool(m.opt("keyword_absent")),
                        follow_redirects=m.opt("follow_redirects", True) is not False,
                        body=m.opt("body", "") if m.method in (Method.POST, Method.PUT) else "")  # fmt: skip
        else:
            spec.update(host=m.target, port=int(m.opt("port") or 0), tls=bool(m.opt("tls")))
        if m.opt("slow_ms"):
            spec["slow_ms"] = int(m.opt("slow_ms"))
        out.append(spec)
    return out


def assignments_rev(assignments: list[dict]) -> str:
    import hashlib

    raw = json.dumps(assignments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def receive_probe_results(server, results) -> int:
    """Results an agent sent with its heartbeat. Only for monitors that currently list this
    server as a location: an agent can't write results for anything else."""
    import uuid

    if not isinstance(results, list) or not results:
        return 0
    wanted = {}
    for r in results[: MAX_PROBES_PER_AGENT * 3]:
        if not isinstance(r, dict):
            continue
        try:
            wanted.setdefault(uuid.UUID(str(r.get("id"))), []).append(r)
        except ValueError:
            continue
    monitors = Monitor.objects.filter(
        pk__in=list(wanted), probe_servers=server, is_enabled=True, kind__in=PROBE_KINDS
    )
    stored = 0
    for monitor in monitors:
        for r in wanted[monitor.pk][-3:]:  # a few per heartbeat at most
            ms = r.get("ms")
            code = r.get("code")
            result = Result(
                ok=r.get("ok") is True,
                degraded=r.get("degraded") is True,
                status_code=code if isinstance(code, int) and 100 <= code <= 599 else None,
                response_ms=ms if isinstance(ms, int) and 0 <= ms < 600_000 else None,
                error=str(r.get("error") or "")[:300],
            )
            record(monitor, result, location=str(server.pk))
            stored += 1
    return stored


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
                next_check_at=heartbeat_deadline(monitor, now)
                if monitor.kind == Kind.HEARTBEAT
                else now + timedelta(seconds=monitor.interval_seconds)
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
