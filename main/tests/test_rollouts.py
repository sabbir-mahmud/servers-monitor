"""3.2: cron schedules and rolling rollouts."""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.monitors.models import Monitor, MonitorStatus
from apps.operations import services
from apps.operations.cron import Cron, CronError
from apps.operations.models import CommandStatus, Job, JobState, Schedule

from .conftest import make_server

pytestmark = pytest.mark.django_db

ACTIONS = {"actions": [{"name": "deploy_api", "timeout": 300}], "logs": [], "shell": False}


# --------------------------------------------------------------------------- cron


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


@pytest.mark.parametrize(
    "expr,after,expected",
    [
        ("0 3 * * *", _utc(2026, 10, 11, 2, 59), _utc(2026, 10, 11, 3, 0)),
        ("0 3 * * *", _utc(2026, 10, 11, 3, 0), _utc(2026, 10, 12, 3, 0)),  # strictly after
        ("*/15 * * * *", _utc(2026, 10, 11, 10, 7), _utc(2026, 10, 11, 10, 15)),
        ("0 9-17/4 * * *", _utc(2026, 10, 11, 14, 0), _utc(2026, 10, 11, 17, 0)),
        (
            "30 2 * * mon",
            _utc(2026, 10, 11, 0, 0),
            _utc(2026, 10, 12, 2, 30),
        ),  # Oct 11 2026 is a Sunday
        ("0 0 1,15 * *", _utc(2026, 10, 2, 0, 0), _utc(2026, 10, 15, 0, 0)),
        ("0 0 13 * fri", _utc(2026, 10, 11, 0, 0), _utc(2026, 10, 13, 0, 0)),  # day OR weekday
        ("0 0 * * 7", _utc(2026, 10, 12, 0, 0), _utc(2026, 10, 18, 0, 0)),  # 7 = Sunday
        ("0 0 29 feb *", _utc(2026, 3, 1, 0, 0), _utc(2028, 2, 29, 0, 0)),
        ("@hourly", _utc(2026, 10, 11, 10, 7), _utc(2026, 10, 11, 11, 0)),
    ],
)
def test_cron_table(expr, after, expected):
    assert Cron(expr).next_after(after, "UTC") == expected


@pytest.mark.parametrize(
    "bad", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "x * * * *"]
)
def test_cron_rejects_bad_expressions(bad):
    with pytest.raises(CronError):
        Cron(bad)


def test_cron_across_daylight_saving():
    ny = "America/New_York"
    # 2026-03-08: 02:00 jumps to 03:00, so a 02:30 job skips that day.
    spring = Cron("30 2 * * *").next_after(_utc(2026, 3, 8, 5, 0), ny)
    assert spring.isoformat() == "2026-03-09T02:30:00-04:00"
    # 2026-11-01: 01:30 happens twice; the job runs once.
    first = Cron("30 1 * * *").next_after(_utc(2026, 11, 1, 4, 0), ny)
    assert first.isoformat() == "2026-11-01T01:30:00-04:00"
    assert Cron("30 1 * * *").next_after(first, ny).date().isoformat() == "2026-11-02"


# --------------------------------------------------------------------------- rollouts


@pytest.fixture
def fleet(db):
    return [make_server(f"web-{i:02d}", capabilities=ACTIONS)[0] for i in range(1, 6)]


@pytest.fixture(autouse=True)
def no_relay(monkeypatch):
    monkeypatch.setattr(services, "dispatch", lambda commands: len(commands))


def _rolling(user, servers, **opts):
    rollout = {
        "strategy": "rolling",
        "batch_size": 2,
        "pause_seconds": 0,
        "stop_on_failure": True,
    } | opts
    job, _ = services.create_job(user, "deploy_api", {}, servers, rollout=rollout)
    return job


def _finish(job, batch, status=CommandStatus.SUCCEEDED):
    job.commands.filter(batch=batch).update(status=status, finished_at=timezone.now())


def _statuses(job):
    return [
        (c.server.name, c.batch, c.status)
        for c in job.commands.select_related("server").order_by("server__name")
    ]


def test_only_the_first_batch_starts(operator, fleet):
    job = _rolling(operator, fleet)
    assert [s for _, b, s in _statuses(job)] == [
        "pending",
        "pending",
        "waiting",
        "waiting",
        "waiting",
    ]
    assert [b for _, b, _ in _statuses(job)] == [0, 0, 1, 1, 2]
    assert services.advance_rollout(job) == "running"  # batch 1 still going


def test_batches_progress_to_completion(operator, fleet):
    job = _rolling(operator, fleet)
    for batch in range(3):
        _finish(job, batch)
        job.refresh_from_db()
        result = services.advance_rollout(job)
        job.refresh_from_db()
        if batch < 2:
            assert result == f"released batch {batch + 2}" and job.current_batch == batch + 1
        else:
            assert result == "completed" and job.state == JobState.COMPLETED


def test_failure_halts_and_resume_continues(operator, fleet):
    job = _rolling(operator, fleet)
    job.commands.filter(server__name="web-01").update(status=CommandStatus.FAILED)
    job.commands.filter(server__name="web-02").update(status=CommandStatus.SUCCEEDED)
    assert services.advance_rollout(job) == "halted"
    job.refresh_from_db()
    assert job.state == JobState.HALTED and "web-01" in job.halt_reason
    assert job.commands.filter(status=CommandStatus.WAITING).count() == 3  # nothing more started
    assert AuditLog.objects.filter(action="job.halted").exists()
    assert services.resume_job(operator, job)
    job.refresh_from_db()
    assert services.advance_rollout(job) == "released batch 2"


def test_failures_ignored_when_not_stopping(operator, fleet):
    job = _rolling(operator, fleet, stop_on_failure=False)
    _finish(job, 0, CommandStatus.FAILED)
    job.refresh_from_db()
    assert services.advance_rollout(job) == "released batch 2"


def test_pause_between_batches(operator, fleet):
    job = _rolling(operator, fleet, pause_seconds=60)
    _finish(job, 0)
    now = timezone.now()
    job.refresh_from_db()
    assert services.advance_rollout(job, now) == "pausing"
    job.refresh_from_db()
    assert services.advance_rollout(job, now + timedelta(seconds=30)) == "pausing"
    assert services.advance_rollout(job, now + timedelta(seconds=61)) == "released batch 2"


def test_health_gate_waits_then_halts(operator, fleet):
    shop = Monitor.objects.create(
        name="Shop", url="https://shop.example.com", status=MonitorStatus.DOWN
    )
    job = _rolling(operator, fleet, health_gates=[shop])
    _finish(job, 0)
    now = timezone.now()
    job.refresh_from_db()
    assert services.advance_rollout(job, now) == "gated"
    # Up, but only checked before the batch finished: still waiting for a fresh check.
    Monitor.objects.filter(pk=shop.pk).update(
        status=MonitorStatus.UP, last_checked_at=now - timedelta(seconds=5)
    )
    job.refresh_from_db()
    assert services.advance_rollout(job, now + timedelta(seconds=10)) == "gated"
    Monitor.objects.filter(pk=shop.pk).update(last_checked_at=now + timedelta(seconds=20))
    assert services.advance_rollout(job, now + timedelta(seconds=25)) == "released batch 2"
    # Next batch: the app stays down past the gate timeout.
    _finish(job, 1)
    Monitor.objects.filter(pk=shop.pk).update(status=MonitorStatus.DOWN)
    job.refresh_from_db()
    later = now + timedelta(minutes=1)
    assert services.advance_rollout(job, later) == "gated"
    job.refresh_from_db()
    assert services.advance_rollout(job, later + timedelta(minutes=11)) == "halted"
    job.refresh_from_db()
    assert "Shop not up" in job.halt_reason


def test_abort_cancels_waiting_batches(client, operator, fleet):
    job = _rolling(operator, fleet)
    client.force_login(operator)
    client.post(reverse("operations:job_control", args=[job.pk, "abort"]))
    job.refresh_from_db()
    assert job.state == JobState.ABORTED
    assert job.commands.filter(status=CommandStatus.CANCELLED).count() == 3
    assert (
        job.commands.filter(status=CommandStatus.PENDING).count() == 2
    )  # already sent: they finish


def test_job_form_starts_a_rolling_job(client, operator, fleet):
    client.force_login(operator)
    resp = client.post(reverse("operations:job_create"), {
        "action": "deploy_api", "servers": [s.pk for s in fleet], "rolling": "on", "batch_size": 3,
        "pause_seconds": 0, "stop_on_failure": "on"})  # fmt: skip
    assert resp.status_code == 302
    job = Job.objects.get()
    assert job.strategy == "rolling" and job.batch_size == 3
    page = client.get(reverse("operations:job_detail", args=[job.pk])).content.decode()
    assert "Batch 2" in page and "Abort" in page


# --------------------------------------------------------------------------- schedules


def _schedule(user, servers=(), **kw):
    s = Schedule.objects.create(name=kw.pop("name", "nightly"), action="deploy_api", cron=kw.pop("cron", "0 3 * * *"),
                                created_by=user, next_run_at=kw.pop("next_run_at", timezone.now()), **kw)  # fmt: skip
    s.servers.set(servers)
    return s


def test_schedule_fires_once_per_slot(admin_user, fleet):
    sched = _schedule(admin_user, fleet[:2])
    now = timezone.now()
    assert services.run_due_schedules(now) == 1
    assert services.run_due_schedules(now) == 0  # same slot: not again
    sched.refresh_from_db()
    assert sched.next_run_at > now and sched.last_run_at == now
    job = Job.objects.get()
    assert job.schedule == sched and job.note == "Scheduled: nightly" and job.commands.count() == 2


def test_missed_slots_run_once(admin_user, fleet):
    _schedule(
        admin_user, fleet[:1], cron="*/5 * * * *", next_run_at=timezone.now() - timedelta(hours=3)
    )
    assert services.run_due_schedules() == 1 and Job.objects.count() == 1


def test_targets_resolve_groups_and_environments_at_run_time(admin_user, fleet):
    from apps.fleet.models import ServerGroup

    api = ServerGroup.objects.create(name="api")
    fleet[3].groups.add(api)
    sched = _schedule(admin_user, [fleet[0]], environments=["staging"])
    sched.groups.add(api)
    fleet[4].environment = "staging"
    fleet[4].save()
    assert [s.name for s in sched.targets()] == ["web-01", "web-04", "web-05"]


def test_failing_schedule_records_why(admin_user):
    sched = _schedule(admin_user, [make_server("old", capabilities={"actions": []})[0]])
    assert services.run_due_schedules() == 0
    sched.refresh_from_db()
    assert "action not offered" in sched.last_error


def test_schedule_form_admin_only(client, operator, admin_user, fleet):
    client.force_login(operator)
    assert client.get(reverse("operations:schedule_create")).status_code == 403
    client.force_login(admin_user)
    resp = client.post(reverse("operations:schedule_create"), {
        "sched_name": "Weekly deploy", "cron": "0 2 * * sun", "action": "deploy_api",
        "servers": [fleet[0].pk], "is_enabled": "on"})  # fmt: skip
    assert resp.status_code == 302
    sched = Schedule.objects.get()
    assert sched.next_run_at.astimezone(UTC).weekday() == 6 and sched.next_run_at.hour == 2
    bad = client.post(reverse("operations:schedule_create"), {"sched_name": "x", "cron": "61 * * * *",
                                                              "action": "deploy_api", "servers": [fleet[0].pk]})  # fmt: skip
    assert "cron" in bad.context["form"].errors
