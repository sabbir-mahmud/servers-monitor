from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind
from apps.alerts.services import sweep
from apps.fleet.models import Health, Server
from apps.operations import services
from apps.operations.models import Command, CommandStatus

from .conftest import make_server

pytestmark = pytest.mark.django_db


def _request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _seen(server, seconds_ago):
    Server.objects.filter(pk=server.pk).update(
        last_seen_at=timezone.now() - timedelta(seconds=seconds_ago), last_health=Health.ONLINE
    )
    server.refresh_from_db()


def test_health_thresholds(server_and_token):
    server, _ = server_and_token  # 30 s interval, late after 2×+10 s, offline after 6×+10 s
    assert server.health == Health.PENDING
    _seen(server, 20)
    assert server.health == Health.ONLINE
    _seen(server, 100)
    assert server.health == Health.LATE
    _seen(server, 200)
    assert server.health == Health.OFFLINE


def test_sweep_opens_one_offline_alert_and_heartbeat_resolves_it(agent, server_and_token):
    server, _ = server_and_token
    _seen(server, 500)
    sweep()
    sweep()
    alert = Alert.objects.get(kind=AlertKind.OFFLINE)
    server.refresh_from_db()
    assert server.last_health == Health.OFFLINE and alert.is_open
    agent.heartbeat()
    alert.refresh_from_db()
    assert not alert.is_open


def test_sweep_marks_silent_running_commands_lost(server_and_token, operator):
    server, _ = server_and_token
    server.capabilities = {"actions": [{"name": "deploy_api", "timeout": 60}]}
    server.save()
    job, _ = services.create_job(_request(operator), "deploy_api", {}, [server])
    services.claim_commands(server)
    Command.objects.update(delivered_at=timezone.now() - timedelta(seconds=60 + 121))
    sweep()
    assert Command.objects.get().status == CommandStatus.LOST


def test_create_job_skips_servers_without_the_action(operator):
    a, _ = make_server("a", capabilities={"actions": [{"name": "deploy_api"}]})
    b, _ = make_server("b", capabilities={"actions": []})
    job, skipped = services.create_job(_request(operator), "deploy_api", {}, [a, b])
    assert list(job.commands.values_list("server__name", flat=True)) == ["a"]
    assert [(s.name, r) for s, r in skipped] == [("b", "action not offered by agent")]


def test_create_job_fails_when_no_server_is_eligible(operator):
    a, _ = make_server("a")
    with pytest.raises(services.JobError):
        services.create_job(_request(operator), "deploy_api", {}, [a])


def test_tail_log_only_for_declared_logs_and_lines_clamped(operator):
    a, _ = make_server("a", capabilities={"logs": ["app"]})
    job, _ = services.create_job(
        _request(operator), "tail_log", {"name": "app", "lines": 99999}, [a]
    )
    assert job.args == {"name": "app", "lines": services.MAX_TAIL_LINES}
    with pytest.raises(services.JobError):
        services.create_job(_request(operator), "tail_log", {"name": "/etc/shadow"}, [a])


def test_shell_requires_permission_and_agent_opt_in(operator, admin_user):
    on, _ = make_server("on", capabilities={"shell": True})
    off, _ = make_server("off", capabilities={"shell": False})
    with pytest.raises(services.JobError, match="permission"):
        services.create_job(_request(operator), "shell", {"command": "uptime"}, [on])
    job, skipped = services.create_job(
        _request(admin_user), "shell", {"command": "uptime"}, [on, off]
    )
    assert job.commands.count() == 1 and skipped[0][0] == off


def test_cancel_only_pending(operator):
    a, _ = make_server("a", capabilities={"actions": [{"name": "x"}]})
    job, _ = services.create_job(_request(operator), "x", {}, [a])
    cmd = job.commands.get()
    assert services.cancel_command(_request(operator), cmd)
    assert not services.cancel_command(_request(operator), cmd)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.CANCELLED
    assert services.claim_commands(a) == []
