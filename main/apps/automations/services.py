"""Run lifecycle: request (by hand, schedule, push, redeploy) -> approval? -> queue -> run
(one job, a command per server) -> result.

One run per automation is active at a time; later requests queue. When the active run
finishes, the next one starts: runs started by hand go in order, while queued scheduled
runs and pushes collapse into the newest one (the older ones are marked skipped), so five
quick pushes cause at most two deploys and a slow nightly job never piles up.
"""

import logging
import secrets

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record
from apps.core import crypto
from apps.operations import services as ops
from apps.operations.models import ACTIVE_STATUSES, CommandStatus, JobState, Strategy

from .models import COALESCING, Automation, Kind, Run, RunStatus, Trigger

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------ webhook secret


def new_secret() -> str:
    return crypto.encrypt(secrets.token_urlsafe(32))


def secret_of(automation: Automation) -> str:
    return crypto.decrypt(automation.webhook_secret) if automation.webhook_secret else ""


# ------------------------------------------------------------------------------ runs


def can_approve(user) -> bool:
    return bool(user and user.has_perm("automations.approve_run"))


def request_run(automation: Automation, *, trigger: str, user=None, request=None,
                servers=None, note: str = "", sha: str = "", message: str = "",
                author: str = "", delivery_id: str = "") -> Run:  # fmt: skip
    """Record a run and start it unless it has to wait. Pushes and scheduled runs of an
    automation that needs approval wait for an Admin; so does a run by hand from someone
    who couldn't approve it themselves."""
    needs_approval = automation.require_approval and (
        trigger in (Trigger.PUSH, Trigger.SCHEDULE) or not can_approve(user)
    )
    run = Run.objects.create(
        automation=automation, trigger=trigger, note=note[:200], sha=sha,
        message=message[:300], author=author[:100], delivery_id=delivery_id[:100],
        status=RunStatus.WAITING if needs_approval else RunStatus.QUEUED,
        requested_by=user if getattr(user, "pk", None) else None,
    )  # fmt: skip
    if servers:
        run.servers.set(servers)
    record(request or user, "automation.run", target=f"{automation.name} {_label(run)}".strip(),
           account=automation.account, trigger=trigger, status=run.status)  # fmt: skip
    if run.status == RunStatus.QUEUED:
        kick(automation)
        run.refresh_from_db()
    return run


def _label(run: Run) -> str:
    return run.short_sha if run.automation.kind == Kind.DEPLOY else ""


def decide(request, run: Run, approve: bool) -> bool:
    updated = Run.objects.filter(pk=run.pk, status=RunStatus.WAITING).update(
        status=RunStatus.QUEUED if approve else RunStatus.REJECTED,
        decided_by=request.user, decided_at=timezone.now(),
        finished_at=None if approve else timezone.now(),
    )  # fmt: skip
    if not updated:
        return False
    record(request, "automation.approve" if approve else "automation.reject",
           target=f"{run.automation.name} {_label(run)}".strip())  # fmt: skip
    if approve:
        kick(run.automation)
    return True


def kick(automation: Automation) -> Run | None:
    """Start the next queued run if none is running."""
    with transaction.atomic():
        # Serialise per automation, so two processes never start two runs.
        Automation.objects.select_for_update().filter(pk=automation.pk).first()
        if automation.runs.filter(status=RunStatus.RUNNING).exists():
            return None
        queued = list(automation.runs.filter(status=RunStatus.QUEUED).order_by("created_at"))
        if not queued:
            return None
        nxt = queued[0]
        if nxt.trigger in COALESCING:
            alike = [r for r in queued if r.trigger in COALESCING]
            nxt = alike[-1]
            if stale := [r.pk for r in alike[:-1]]:
                Run.objects.filter(pk__in=stale).update(
                    status=RunStatus.SKIPPED, finished_at=timezone.now()
                )
        _start(nxt)
    return nxt


def _runner(run: Run):
    """Whose permissions the job runs with: whoever approved or asked; for automatic runs,
    whoever last saved the automation (so losing their role stops it)."""
    a = run.automation
    return run.decided_by or run.requested_by or a.updated_by or a.created_by


def _start(run: Run) -> None:
    a = run.automation
    chosen = list(run.servers.filter(is_enabled=True).order_by("name"))
    servers = chosen or list(a.targets())
    user = _runner(run)
    rollout = None
    if a.strategy == Strategy.ROLLING:
        rollout = {"strategy": Strategy.ROLLING, "batch_size": a.batch_size,
                   "pause_seconds": a.pause_seconds, "stop_on_failure": a.stop_on_failure,
                   "health_gates": list(a.health_gates.all())}  # fmt: skip
    run.started_at = timezone.now()
    action, args = a.job_spec(sha=run.sha)
    try:
        if not servers:
            raise ops.JobError("No servers to run on.")
        job, skipped = ops.create_job(
            user, action, args, servers, note=(run.note or _note(run))[:200],
            rollout=rollout, account=a.account, automation=a,
        )  # fmt: skip
    except ops.JobError as exc:
        run.status, run.error, run.finished_at = RunStatus.FAILED, str(exc)[:500], run.started_at
        run.save(update_fields=["status", "error", "started_at", "finished_at"])
        record(user, "automation.failed", target=a.name, account=a.account, error=run.error)
        return
    run.job, run.status = job, RunStatus.RUNNING
    if skipped:
        run.error = "; ".join(f"{s.name}: {r}" for s, r in skipped)[:500]
    run.save(update_fields=["job", "status", "error", "started_at"])


def _note(run: Run) -> str:
    a = run.automation
    if a.kind == Kind.DEPLOY:
        return f"{a.name}: {run.message or run.short_sha}"
    return f"{run.get_trigger_display()}: {a.name}"


def outcome(run: Run) -> str | None:
    """The final status a running run has reached, or None while it's still going."""
    job = run.job
    if job is None:
        return RunStatus.FAILED
    statuses = list(job.commands.values_list("status", flat=True))
    if job.state in (JobState.HALTED, JobState.ABORTED):
        if any(s in ACTIVE_STATUSES for s in statuses):
            return None  # let the servers already running it finish
        return RunStatus.FAILED
    if any(s in ACTIVE_STATUSES or s == CommandStatus.WAITING for s in statuses):
        return None
    if statuses and all(s == CommandStatus.SUCCEEDED for s in statuses):
        return RunStatus.SUCCEEDED
    return RunStatus.FAILED


def sync() -> int:
    """Settle running runs whose servers have all reported, then start what's next.
    Called by the scheduler loop every few seconds. Returns how many settled."""
    settled = 0
    for run in Run.objects.filter(status=RunStatus.RUNNING).select_related("automation", "job"):
        final = outcome(run)
        if final is None:
            continue
        if Run.objects.filter(pk=run.pk, status=RunStatus.RUNNING).update(
            status=final, finished_at=timezone.now()
        ):
            settled += 1
            if final == RunStatus.FAILED:
                record(None, "automation.failed", target=run.automation.name,
                       account=run.automation.account)  # fmt: skip
        kick(run.automation)
    return settled


# ------------------------------------------------------------------------- schedules


def next_run(automation: Automation, after=None):
    if not (automation.cron and automation.is_enabled):
        return None
    return ops.schedule_next(automation, after)


def run_due(now=None) -> int:
    """Start every scheduled automation whose time has come. Missed slots (relay down,
    dashboard restarting) run once, not once per slot."""
    now = now or timezone.now()
    started = 0
    for a in Automation.objects.filter(is_enabled=True, next_run_at__lte=now).exclude(cron=""):
        # Claim the slot first, so two scheduler processes never run it twice.
        claimed = Automation.objects.filter(pk=a.pk, next_run_at=a.next_run_at).update(
            next_run_at=next_run(a, now), last_run_at=now
        )
        if not claimed:
            continue
        run = request_run(a, trigger=Trigger.SCHEDULE)
        Automation.objects.filter(pk=a.pk).update(
            last_error=run.error if run.status == RunStatus.FAILED else ""
        )
        started += 1
    return started


# --------------------------------------------------------------------------- deploys


def server_commits(automation: Automation) -> list[dict]:
    """Each target server and the commit its checkout is on (from heartbeats)."""
    rows = []
    for server in automation.targets():
        app = server.app(automation.app)
        rows.append({"server": server, "app": app, "sha": (app or {}).get("sha", "")})
    return rows
