"""Command lifecycle: create (fan-out), claim (agent poll), progress/result, cancel, expire.

Validation happens twice on purpose. Main refuses actions a server never advertised;
the agent independently refuses anything not in its local allowlist. A compromised
dashboard can therefore only trigger what each server's own config already permits."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record

from .models import ACTIVE_STATUSES, Command, CommandStatus, Job

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
    return job, skipped


def claim_commands(server) -> list[dict]:
    """Hand the agent its queued commands and mark them delivered, exactly once.

    SKIP LOCKED: if two polls from one agent overlap (slow network, restart), each command
    goes to exactly one of them instead of the second poll blocking or double-delivering.
    """
    if not server.is_enabled:
        return []
    now = timezone.now()
    with transaction.atomic():
        pending = list(
            Command.objects.select_for_update(skip_locked=True)
            .filter(server=server, status=CommandStatus.PENDING)
            .order_by("created_at")[: settings.MONITOR_COMMANDS_PER_POLL]
        )
        live = [c for c in pending if c.deliver_by >= now]
        stale = [c.pk for c in pending if c.deliver_by < now]
        if stale:
            Command.objects.filter(pk__in=stale).update(
                status=CommandStatus.EXPIRED, finished_at=now
            )
        if live:
            Command.objects.filter(pk__in=[c.pk for c in live]).update(
                status=CommandStatus.RUNNING, delivered_at=now
            )
    return [
        {"id": str(c.pk), "action": c.action, "args": c.args, "timeout": c.timeout_seconds}
        for c in live
    ]


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
    """Only queued commands can be cancelled; a running deploy is the agent's to finish."""
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
    expired = Command.objects.filter(status=CommandStatus.PENDING, deliver_by__lt=now).update(
        status=CommandStatus.EXPIRED, finished_at=now
    )
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
