"""Deploy lifecycle: request (push, Deploy now, redeploy) -> approve? -> queue -> run (a job
of `_deploy` commands, one per server) -> result.

One deploy per project runs at a time. While one runs, newer requests queue; when it
finishes only the newest queued one starts and the older ones are marked superseded, so
five quick pushes cause at most two deploys.
"""

import logging
import secrets

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record
from apps.core import crypto
from apps.operations import services as ops
from apps.operations.models import ACTIVE_STATUSES, CommandStatus, JobState, Strategy

from .models import Deployment, DeployMode, DeployStatus, Project, Source

logger = logging.getLogger(__name__)


def new_secret() -> str:
    return crypto.encrypt(secrets.token_urlsafe(32))


def secret_of(project: Project) -> str:
    return crypto.decrypt(project.webhook_secret)


def can_approve(user) -> bool:
    return bool(user and user.has_perm("deploys.approve_deployment"))


def request_deploy(project: Project, *, source: str, sha: str = "", message: str = "",
                   author: str = "", user=None, delivery_id: str = "",
                   request=None) -> Deployment:  # fmt: skip
    """Record a deploy and start it if nothing has to wait. Pushes to an approval project
    wait for an Admin; so does Deploy now by someone who couldn't approve it themselves."""
    needs_approval = project.mode == DeployMode.APPROVAL and (
        source == Source.PUSH or not can_approve(user)
    )
    dep = Deployment.objects.create(
        project=project, sha=sha, message=message[:300], author=author[:100], source=source,
        status=DeployStatus.WAITING if needs_approval else DeployStatus.QUEUED,
        steps=project.steps, delivery_id=delivery_id[:100],
        requested_by=user if getattr(user, "pk", None) else None,
    )  # fmt: skip
    record(request or user, "deploy.request", target=f"{project.name} {dep.short_sha}",
           account=project.account, source=source, status=dep.status)  # fmt: skip
    if dep.status == DeployStatus.QUEUED:
        kick(project)
        dep.refresh_from_db()
    return dep


def decide(request, dep: Deployment, approve: bool) -> bool:
    with transaction.atomic():
        updated = Deployment.objects.filter(pk=dep.pk, status=DeployStatus.WAITING).update(
            status=DeployStatus.QUEUED if approve else DeployStatus.REJECTED,
            decided_by=request.user, decided_at=timezone.now(),
            finished_at=None if approve else timezone.now(),
        )  # fmt: skip
    if not updated:
        return False
    record(request, "deploy.approve" if approve else "deploy.reject",
           target=f"{dep.project.name} {dep.short_sha}")  # fmt: skip
    if approve:
        kick(dep.project)
    return True


def kick(project: Project) -> Deployment | None:
    """Start the newest queued deploy if none is running; skip the older queued ones."""
    with transaction.atomic():
        Project.objects.select_for_update().filter(pk=project.pk).first()  # serialise per project
        if project.deployments.filter(status=DeployStatus.RUNNING).exists():
            return None
        queued = list(
            project.deployments.filter(status=DeployStatus.QUEUED).order_by("-created_at")
        )
        if not queued:
            return None
        newest, older = queued[0], queued[1:]
        if older:
            Deployment.objects.filter(pk__in=[d.pk for d in older]).update(
                status=DeployStatus.SUPERSEDED, finished_at=timezone.now()
            )
        _start(newest)
    return newest


def _start(dep: Deployment) -> None:
    project = dep.project
    servers = list(project.servers.filter(is_enabled=True).order_by("name"))
    # Who it runs as, for the job's permissions: whoever approved or asked, else the
    # project's author (pushes). Losing their role stops the project's pushes deploying.
    user = dep.decided_by or dep.requested_by or project.created_by
    rollout = None
    if project.strategy == Strategy.ROLLING:
        rollout = {"strategy": Strategy.ROLLING, "batch_size": project.batch_size,
                   "pause_seconds": 30, "stop_on_failure": project.stop_on_failure,
                   "health_gates": list(project.health_gates.all())}  # fmt: skip
    dep.started_at = timezone.now()
    try:
        if not servers:
            raise ops.JobError("The project has no servers to deploy to.")
        job, skipped = ops.create_job(
            user, "_deploy", {"app": project.app, "sha": dep.sha, "steps": dep.steps},
            servers, note=f"{project.name}: {dep.message or dep.short_sha}"[:200],
            rollout=rollout, account=project.account,
        )  # fmt: skip
    except ops.JobError as exc:
        dep.status, dep.error, dep.finished_at = DeployStatus.FAILED, str(exc)[:500], dep.started_at
        dep.save(update_fields=["status", "error", "started_at", "finished_at"])
        record(user, "deploy.failed", target=f"{project.name} {dep.short_sha}",
               account=project.account, error=dep.error)  # fmt: skip
        return
    dep.job, dep.status = job, DeployStatus.RUNNING
    if skipped:
        dep.error = "; ".join(f"{s.name}: {r}" for s, r in skipped)[:500]
    dep.save(update_fields=["job", "status", "error", "started_at"])


def outcome(dep: Deployment) -> str | None:
    """The final status a running deploy has reached, or None while it's still going."""
    job = dep.job
    if job is None:
        return DeployStatus.FAILED
    statuses = list(job.commands.values_list("status", flat=True))
    if job.state in (JobState.HALTED, JobState.ABORTED):
        if any(s in ACTIVE_STATUSES for s in statuses):
            return None  # let the servers already running it finish
        return DeployStatus.FAILED
    if any(s in ACTIVE_STATUSES or s == CommandStatus.WAITING for s in statuses):
        return None
    if statuses and all(s == CommandStatus.SUCCEEDED for s in statuses):
        return DeployStatus.SUCCEEDED
    return DeployStatus.FAILED


def sync() -> int:
    """Settle running deploys whose servers have all reported, then start what's next.
    Called by the scheduler loop every few seconds. Returns how many settled."""
    settled = 0
    for dep in Deployment.objects.filter(status=DeployStatus.RUNNING).select_related(
        "project", "job"
    ):
        final = outcome(dep)
        if final is None:
            continue
        if Deployment.objects.filter(pk=dep.pk, status=DeployStatus.RUNNING).update(
            status=final, finished_at=timezone.now()
        ):
            settled += 1
            record(None, f"deploy.{final}", target=f"{dep.project.name} {dep.short_sha}",
                   account=dep.project.account)  # fmt: skip
        kick(dep.project)
    return settled


def server_commits(project: Project) -> list[dict]:
    """Each target server and the commit its checkout is on (from heartbeats)."""
    rows = []
    for server in project.servers.order_by("name"):
        app = server.app(project.app)
        rows.append({"server": server, "app": app, "sha": (app or {}).get("sha", "")})
    return rows
