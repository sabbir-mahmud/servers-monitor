"""Command lifecycle: create (fan-out), dispatch (relay push), ack, progress/result, cancel,
expire.

Validation happens twice on purpose. Main refuses actions a server never advertised;
the agent independently refuses anything not in its local allowlist. A compromised
dashboard can therefore only trigger what each server's own config already permits."""

import logging
from datetime import timedelta

import redis
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record

from .models import ACTIVE_STATUSES, Command, CommandStatus, Job, JobState, Strategy


def _relay():
    from apps.agent_api import relay  # late: agent_api imports this module too

    return relay


logger = logging.getLogger(__name__)

BUILTIN_ACTIONS = {"tail_log", "tail_follow", "restart_service", "shell", "_upgrade"}
UPGRADE_SECONDS = 1200  # download, verify, reinstall, restart, test heartbeat
FOLLOW_SECONDS = 600  # the agent's own maximum too
JOURNAL_PREFIX = "journal:"  # tail_follow of a systemd service's journal instead of a file
MAX_TAIL_LINES = 2000
MAX_SHELL_LENGTH = 4000
# Seconds past an action's timeout before a silent delivered command counts as lost.
LOST_GRACE_SECONDS = 120
FINAL_STATUSES = {CommandStatus.SUCCEEDED, CommandStatus.FAILED}


class JobError(Exception):
    pass


def clean_args(action: str, raw: dict) -> dict:
    """Normalise operator input to the exact argument shape the agent accepts."""
    if action == "_upgrade":
        from apps.fleet import agentdist

        args = agentdist.upgrade_args()  # never from the request: only what's deployed here
        if args is None:
            raise JobError("Remote upgrades need AGENT_REF pinned to a release tag (vX.Y.Z) "
                           "and the agent files next to the dashboard.")  # fmt: skip
        return args
    if action == "tail_follow":
        name = str(raw.get("name", "")).strip()
        if not name:
            raise JobError("Choose a log.")
        line_filter = str(raw.get("filter") or "").strip()
        if len(line_filter) > 200:
            raise JobError("The filter is limited to 200 characters.")
        journal = name.startswith(JOURNAL_PREFIX)
        return {"source": "journal" if journal else "log",
                "name": name.removeprefix(JOURNAL_PREFIX), "filter": line_filter,
                "seconds": FOLLOW_SECONDS}  # fmt: skip
    if action == "tail_log":
        if str(raw.get("name", "")).startswith(JOURNAL_PREFIX):
            raise JobError("Service journals can only be followed: press Follow.")
        try:
            lines = int(raw.get("lines") or 200)
        except TypeError, ValueError:
            raise JobError("Lines must be a number.") from None
        name = str(raw.get("name", "")).strip()
        if not name:
            raise JobError("Choose a log.")
        return {"name": name, "lines": max(1, min(lines, MAX_TAIL_LINES))}
    if action == "restart_service":
        name = str(raw.get("name", "")).strip()
        if not name:
            raise JobError("Choose a service.")
        return {"name": name}
    if action == "shell":
        command = str(raw.get("command", "")).strip()
        if not command:
            raise JobError("Enter a command.")
        if len(command) > MAX_SHELL_LENGTH:
            raise JobError(f"Commands are limited to {MAX_SHELL_LENGTH} characters.")
        return {"command": command}
    return {}  # custom actions take no arguments: their argv is fixed in the agent config


def reason_unavailable(server, action: str, args: dict) -> str | None:
    """Why `action` cannot run on `server`, or None if it can."""
    if not server.is_enabled:
        return "server disabled"
    if action == "shell":
        return None if server.shell_enabled else "shell disabled in agent config"
    if action == "tail_log":
        return (
            None if args.get("name") in server.log_names else f"no log named {args.get('name')!r}"
        )
    if action == "_upgrade":
        from apps.fleet import agentdist

        if not server.signing_salt:
            return "unsigned (legacy) agent: rotate its credentials first"
        if server.agent_version == agentdist.agent_version():
            return f"already on v{server.agent_version}"
        if "remote_upgrade" not in (server.capabilities.get("features") or []):
            return "remote upgrades are off on the server (sudo smctl remote-upgrade on)"
        return None
    if action == "tail_follow":
        if "log_follow" not in (server.capabilities.get("features") or []):
            return "agent too old to follow logs (upgrade it)"
        if args.get("source") == "journal":
            ok = args.get("name") in server.journal_services
            return None if ok else f"no systemd service {args.get('name')!r}"
        return (
            None if args.get("name") in server.log_names else f"no log named {args.get('name')!r}"
        )
    if action == "restart_service":
        if args.get("name") in server.restartable_services:
            return None
        return f"no restartable service {args.get('name')!r}"
    return None if action in server.action_names else "action not offered by agent"


def _timeout_for(server, action: str) -> int:
    for spec in server.actions:
        if spec.get("name") == action:
            return int(spec.get("timeout") or 300)
    if action == "tail_follow":
        return FOLLOW_SECONDS + 30
    if action == "_upgrade":
        return UPGRADE_SECONDS
    return 60 if action in ("tail_log", "restart_service") else 300


def create_job(
    request,
    action: str,
    raw_args: dict,
    servers,
    note: str = "",
    rollout: dict | None = None,
    schedule=None,
) -> tuple[Job, list]:
    """Queue `action` on every eligible server. Returns (job, [(server, skip reason)]).
    `request` may be a request or a bare user (scheduled runs). With a rolling `rollout`
    ({batch_size, pause_seconds, stop_on_failure, health_gates}) only the first batch is
    sent; advance_rollouts() releases the rest."""
    user = getattr(request, "user", request)
    if action == "shell" and not (user and user.has_perm("operations.run_shell")):
        raise JobError("You do not have permission to run shell commands.")
    if action == "_upgrade" and not (user and user.has_perm("fleet.change_server")):
        raise JobError("Only Admins can upgrade agents.")
    args = clean_args(action, raw_args)
    eligible, skipped = [], []
    for server in servers:
        reason = reason_unavailable(server, action, args)
        (skipped.append((server, reason)) if reason else eligible.append(server))
    if not eligible:
        raise JobError(
            "No selected server can run this action: "
            + "; ".join(f"{s.name}: {r}" for s, r in skipped)
        )

    rolling = bool(rollout) and rollout.get("strategy", Strategy.ROLLING) == Strategy.ROLLING
    batch_size = max(1, int((rollout or {}).get("batch_size") or 1))
    if rolling:
        eligible.sort(key=lambda s: s.name)
    deliver_by = timezone.now() + timedelta(seconds=settings.MONITOR_COMMAND_DELIVERY_SECONDS)
    with transaction.atomic():
        job = Job.objects.create(
            action=action, args=args, note=note, created_by=user if getattr(user, "pk", None) else None,
            schedule=schedule,
            **({"strategy": Strategy.ROLLING, "batch_size": batch_size,
                "pause_seconds": int(rollout.get("pause_seconds") or 0),
                "stop_on_failure": bool(rollout.get("stop_on_failure", True)),
                "gate_timeout_seconds": int(rollout.get("gate_timeout_seconds") or 600)} if rolling else {}),
        )  # fmt: skip
        if rolling and rollout.get("health_gates"):
            job.health_gates.set(rollout["health_gates"])
        Command.objects.bulk_create(
            Command(
                job=job,
                server=s,
                action=action,
                args=args,
                timeout_seconds=_timeout_for(s, action),
                deliver_by=deliver_by,
                batch=i // batch_size if rolling else 0,
                status=CommandStatus.WAITING
                if rolling and i >= batch_size
                else CommandStatus.PENDING,
            )
            for i, s in enumerate(eligible)
        )
        record(
            request,
            "job.create",
            target=job.summary,
            server=eligible[0] if len(eligible) == 1 else None,
            job=str(job.pk),
            servers=[s.name for s in eligible],
            skipped={s.name: r for s, r in skipped},
            **({"rolling": f"batches of {batch_size}"} if rolling else {}),
            **({"schedule": schedule.name} if schedule else {}),
        )
        transaction.on_commit(
            lambda: dispatch(
                list(job.commands.filter(status=CommandStatus.PENDING).select_related("server"))
            )
        )
    return job, skipped


# --------------------------------------------------------------------------- rollouts

FAILED_STATUSES = (
    CommandStatus.FAILED,
    CommandStatus.LOST,
    CommandStatus.EXPIRED,
    CommandStatus.CANCELLED,
)


def _halt(job: Job, reason: str) -> None:
    Job.objects.filter(pk=job.pk).update(state=JobState.HALTED, halt_reason=reason[:300])
    record(None, "job.halted", target=job.summary, job=str(job.pk), reason=reason)


def advance_rollout(job: Job, now=None) -> str:
    """Move one rolling job forward. Returns what happened (for logs and tests)."""
    from apps.monitors.models import MonitorStatus

    now = now or timezone.now()
    batch = list(job.commands.filter(batch=job.current_batch))
    if any(c.status in ACTIVE_STATUSES for c in batch):
        return "running"
    failed = [c for c in batch if c.status in FAILED_STATUSES]
    if failed and job.stop_on_failure and job.current_batch > job.accept_failures_through:
        names = ", ".join(sorted(c.server.name for c in failed))
        _halt(
            job,
            f"Batch {job.current_batch + 1}: failed on {names}. Later batches were not started.",
        )
        return "halted"
    if not job.commands.filter(status=CommandStatus.WAITING).exists():
        Job.objects.filter(pk=job.pk).update(state=JobState.COMPLETED)
        return "completed"
    if job.batch_done_at is None:
        Job.objects.filter(pk=job.pk).update(batch_done_at=now)
        job.batch_done_at = now
    waited = (now - job.batch_done_at).total_seconds()
    if waited < job.pause_seconds:
        return "pausing"
    gates = list(job.health_gates.all())
    unhealthy = [
        m.name for m in gates
        if m.status not in (MonitorStatus.UP, MonitorStatus.DEGRADED)
        or m.last_checked_at is None or m.last_checked_at < job.batch_done_at  # a check since the batch
    ]  # fmt: skip
    if unhealthy:
        if waited >= job.pause_seconds + job.gate_timeout_seconds:
            _halt(job, f"{', '.join(unhealthy)} not up {job.gate_timeout_seconds // 60} min after batch "
                       f"{job.current_batch + 1}. Later batches were not started.")  # fmt: skip
            return "halted"
        return "gated"
    nxt = job.current_batch + 1
    deliver_by = now + timedelta(seconds=settings.MONITOR_COMMAND_DELIVERY_SECONDS)
    with transaction.atomic():
        job.commands.filter(batch=nxt, status=CommandStatus.WAITING).update(
            status=CommandStatus.PENDING, deliver_by=deliver_by
        )
        Job.objects.filter(pk=job.pk).update(current_batch=nxt, batch_done_at=None)
    dispatch(
        list(job.commands.filter(batch=nxt, status=CommandStatus.PENDING).select_related("server"))
    )
    return f"released batch {nxt + 1}"


def advance_rollouts(now=None) -> int:
    moved = 0
    for job in Job.objects.filter(strategy=Strategy.ROLLING, state=JobState.RUNNING):
        moved += advance_rollout(job, now).startswith("released")
    return moved


def resume_job(request, job: Job) -> bool:
    if job.state != JobState.HALTED:
        return False
    Job.objects.filter(pk=job.pk).update(
        state=JobState.RUNNING, halt_reason="", accept_failures_through=job.current_batch,
        batch_done_at=timezone.now(),  # gates must pass again from now
    )  # fmt: skip
    record(request, "job.resume", target=job.summary, job=str(job.pk))
    return True


def abort_job(request, job: Job) -> int:
    if job.state not in (JobState.RUNNING, JobState.HALTED):
        return 0
    n = job.commands.filter(status=CommandStatus.WAITING).update(
        status=CommandStatus.CANCELLED, finished_at=timezone.now()
    )
    Job.objects.filter(pk=job.pk).update(state=JobState.ABORTED)
    record(request, "job.abort", target=job.summary, job=str(job.pk), cancelled=n)
    return n


# --------------------------------------------------------------------------- schedules


def schedule_next(schedule, after=None):
    from .cron import Cron

    return Cron(schedule.cron).next_after(after or timezone.now(), settings.TIME_ZONE)


def run_due_schedules(now=None) -> int:
    """Start every schedule whose time has come. Missed runs (relay down) run once, not
    once per missed slot."""
    from .models import Schedule

    now = now or timezone.now()
    started = 0
    for sched in Schedule.objects.filter(is_enabled=True, next_run_at__lte=now):
        # Claim it first, so two relay processes never run the same slot.
        claimed = Schedule.objects.filter(pk=sched.pk, next_run_at=sched.next_run_at).update(
            next_run_at=schedule_next(sched, now), last_run_at=now
        )
        if not claimed:
            continue
        rollout = None
        if sched.strategy == Strategy.ROLLING:
            rollout = {"strategy": Strategy.ROLLING, "batch_size": sched.batch_size,
                       "pause_seconds": sched.pause_seconds, "stop_on_failure": sched.stop_on_failure,
                       "health_gates": list(sched.health_gates.all())}  # fmt: skip
        try:
            create_job(sched.created_by, sched.action, sched.args, list(sched.targets()),
                       note=f"Scheduled: {sched.name}", rollout=rollout, schedule=sched)  # fmt: skip
            Schedule.objects.filter(pk=sched.pk).update(last_error="")
            started += 1
        except JobError as exc:
            Schedule.objects.filter(pk=sched.pk).update(last_error=str(exc)[:300])
            record(None, "schedule.failed", target=sched.name, error=str(exc)[:300])
    return started


def dispatch(commands) -> int:
    """Push commands into their agents' relay mailboxes. A command Redis refuses stays in
    the outbox (dispatched_at NULL) and the relay process retries it every few seconds."""
    relay, sent, now = _relay(), 0, timezone.now()
    for command in commands:
        if command.status != CommandStatus.PENDING or command.deliver_by < now:
            continue
        try:
            relay.push_command(command)
        except (redis.RedisError, relay.RelayError, OSError) as exc:
            logger.warning("Relay push failed for %s; will retry: %s", command.pk, exc)
            break  # Redis is down for everyone: leave the rest for the retry loop
        Command.objects.filter(pk=command.pk, dispatched_at__isnull=True).update(dispatched_at=now)
        sent += 1
    return sent


def dispatch_pending() -> int:
    """Outbox retry: commands queued while Redis was unreachable."""
    waiting = (
        Command.objects.filter(
            status=CommandStatus.PENDING,
            dispatched_at__isnull=True,
            deliver_by__gte=timezone.now(),
            server__is_enabled=True,
        )
        .select_related("server")
        .order_by("created_at")[:100]
    )
    return dispatch(list(waiting))


def acknowledge(server, command_id) -> Command | None:
    """The agent popped this command and is starting it."""
    now = timezone.now()
    Command.objects.filter(pk=command_id, server=server, status=CommandStatus.PENDING).update(
        status=CommandStatus.RUNNING, delivered_at=now
    )
    return Command.objects.filter(pk=command_id, server=server).first()


def _append(command: Command, chunk: str) -> None:
    if not chunk:
        return
    limit = settings.MONITOR_COMMAND_OUTPUT_LIMIT
    combined = command.output + chunk
    if len(combined) > limit:
        # Keep the tail: the end of a deploy log is where the error is.
        combined = combined[-limit:]
        command.output_truncated = True
    command.output = combined


def report_progress(server, command_id, status: str, output: str, exit_code) -> Command | None:
    """Apply an agent's progress or final result. Returns None if the command isn't this
    server's (indistinguishable from not found, so agents can't probe other servers)."""
    with transaction.atomic():
        command = Command.objects.select_for_update().filter(pk=command_id, server=server).first()
        if command is None:
            return None
        if command.status != CommandStatus.RUNNING:
            # Late result for a command already marked lost/cancelled: keep the output for
            # the operator but don't resurrect the status.
            if command.status in (CommandStatus.LOST, CommandStatus.CANCELLED) and output:
                _append(command, output)
                command.save(update_fields=["output", "output_truncated"])
            return command
        _append(command, output)
        fields = ["output", "output_truncated"]
        if status in FINAL_STATUSES:
            command.status = status
            command.exit_code = exit_code
            command.finished_at = timezone.now()
            fields += ["status", "exit_code", "finished_at"]
        command.save(update_fields=fields)
    return command


def cancel_command(request, command: Command) -> bool:
    """Only queued commands can be cancelled; a running deploy is the agent's to finish.
    A dispatched command is cancellable only if it's still sitting in the agent's mailbox
    (LREM proves the agent never popped it)."""
    if command.status != CommandStatus.PENDING:
        return False
    if command.dispatched_at is not None:
        try:
            if not _relay().unqueue_command(command):
                return False  # already picked up: it's running now
        except (redis.RedisError, OSError) as exc:
            logger.warning("Could not reach the relay to cancel %s: %s", command.pk, exc)
            return False
    with transaction.atomic():
        updated = Command.objects.filter(pk=command.pk, status=CommandStatus.PENDING).update(
            status=CommandStatus.CANCELLED, finished_at=timezone.now()
        )
        if updated:
            record(
                request,
                "command.cancel",
                target=command.action,
                server=command.server,
                command=str(command.pk),
            )
    return bool(updated)


def upgrade_agents(request, servers, batch_size: int = 0) -> tuple[Job, list]:
    """Upgrade agents to the release this dashboard ships; with `batch_size`, a rolling job
    that stops at the first failure."""
    rollout = None
    if batch_size:
        rollout = {"strategy": Strategy.ROLLING, "batch_size": batch_size, "pause_seconds": 30,
                   "stop_on_failure": True}  # fmt: skip
    return create_job(request, "_upgrade", {}, servers, note="Agent upgrade", rollout=rollout)


def stop_follow(request, command: Command) -> bool:
    """Stop a running log follow: the agent ends it and reports what it had."""
    if command.action != "tail_follow":
        return False
    if command.status == CommandStatus.PENDING:
        return cancel_command(request, command)
    if command.status != CommandStatus.RUNNING:
        return False
    try:
        _relay().push(command.server, {"t": "cancel", "id": str(command.pk)})
    except (redis.RedisError, OSError) as exc:
        logger.warning("Could not reach the relay to stop %s: %s", command.pk, exc)
        return False
    record(request, "command.stop", target=command.job.summary, server=command.server,
           command=str(command.pk))  # fmt: skip
    return True


def expire_commands(now=None) -> int:
    now = now or timezone.now()
    stale = Command.objects.filter(status=CommandStatus.PENDING, deliver_by__lt=now)
    for command in stale.filter(dispatched_at__isnull=False).select_related("server"):
        try:  # tidy the mailbox; the agent also ignores expired jobs on its own
            _relay().unqueue_command(command)
        except redis.RedisError, OSError:
            break
    expired = stale.update(status=CommandStatus.EXPIRED, finished_at=now)
    lost = 0
    running = Command.objects.filter(status=CommandStatus.RUNNING).only(
        "pk", "delivered_at", "timeout_seconds"
    )
    for c in running:
        if c.delivered_at and now > c.delivered_at + timedelta(
            seconds=c.timeout_seconds + LOST_GRACE_SECONDS
        ):
            lost += Command.objects.filter(pk=c.pk, status=CommandStatus.RUNNING).update(
                status=CommandStatus.LOST, finished_at=now
            )
    return expired + lost


def job_counts(job: Job) -> dict:
    counts = {s.value: 0 for s in CommandStatus}
    for c in job.commands.all():
        counts[c.status] += 1
    counts["active"] = sum(counts[s] for s in ACTIVE_STATUSES)
    counts["total"] = sum(counts[s.value] for s in CommandStatus)
    return counts
