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

from .models import ACTIVE_STATUSES, Command, CommandStatus, Job


def _relay():
    from apps.agent_api import relay  # late: agent_api imports this module too

    return relay


logger = logging.getLogger(__name__)

BUILTIN_ACTIONS = {"tail_log", "restart_service", "shell"}
MAX_TAIL_LINES = 2000
MAX_SHELL_LENGTH = 4000
# Seconds past an action's timeout before a silent delivered command counts as lost.
LOST_GRACE_SECONDS = 120
FINAL_STATUSES = {CommandStatus.SUCCEEDED, CommandStatus.FAILED}


class JobError(Exception):
    pass


def clean_args(action: str, raw: dict) -> dict:
    """Normalise operator input to the exact argument shape the agent accepts."""
    if action == "tail_log":
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
    if action == "restart_service":
        if args.get("name") in server.restartable_services:
            return None
        return f"no restartable service {args.get('name')!r}"
    return None if action in server.action_names else "action not offered by agent"


def _timeout_for(server, action: str) -> int:
    for spec in server.actions:
        if spec.get("name") == action:
            return int(spec.get("timeout") or 300)
    return 60 if action in ("tail_log", "restart_service") else 300


def create_job(request, action: str, raw_args: dict, servers, note: str = "") -> tuple[Job, list]:
    """Queue `action` on every eligible server. Returns (job, [(server, skip reason)])."""
    user = request.user
    if action == "shell" and not user.has_perm("operations.run_shell"):
        raise JobError("You do not have permission to run shell commands.")
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

    deliver_by = timezone.now() + timedelta(seconds=settings.MONITOR_COMMAND_DELIVERY_SECONDS)
    with transaction.atomic():
        job = Job.objects.create(action=action, args=args, note=note, created_by=user)
        Command.objects.bulk_create(
            Command(
                job=job,
                server=s,
                action=action,
                args=args,
                timeout_seconds=_timeout_for(s, action),
                deliver_by=deliver_by,
            )
            for s in eligible
        )
        record(
            request,
            "job.create",
            target=job.summary,
            server=eligible[0] if len(eligible) == 1 else None,
            job=str(job.pk),
            servers=[s.name for s in eligible],
            skipped={s.name: r for s, r in skipped},
        )
        transaction.on_commit(lambda: dispatch(list(job.commands.select_related("server"))))
    return job, skipped


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
