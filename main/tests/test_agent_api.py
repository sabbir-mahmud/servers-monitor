import json

import pytest
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind
from apps.fleet.models import Health, MetricSample
from apps.operations.models import CommandStatus, Job

from .conftest import AgentClient, make_server, report

pytestmark = pytest.mark.django_db


def test_heartbeat_rejects_missing_and_wrong_token(client):
    assert (
        client.post("/api/agent/v1/heartbeat/", "{}", content_type="application/json").status_code
        == 401
    )
    bad = AgentClient(client, "smk_wrong")
    assert bad.heartbeat().status_code == 401


def test_heartbeat_stores_snapshot_sample_and_returns_schedule(agent, server_and_token):
    server, _ = server_and_token
    server.report_interval = 60
    server.save(update_fields=["report_interval"])
    resp = agent.heartbeat()
    assert resp.status_code == 200
    body = resp.json()
    assert body["report_interval"] == 60 and body["commands"] == []
    server.refresh_from_db()
    assert server.cpu_percent == 12.5 and server.hostname == "web-01"
    assert server.health == Health.ONLINE
    assert server.action_names == {"deploy_api"}
    assert MetricSample.objects.filter(server=server).count() == 1


def test_heartbeat_validates_and_clamps(agent, server_and_token):
    resp = agent.heartbeat(report(metrics={"cpu_percent": "lots"}))
    assert resp.status_code == 400
    resp = agent.heartbeat(report(metrics={"cpu_percent": 250.0}))
    assert resp.status_code == 200
    server_and_token[0].refresh_from_db()
    assert server_and_token[0].cpu_percent == 100.0


def test_bad_json_is_400(client, server_and_token):
    resp = client.post(
        "/api/agent/v1/heartbeat/",
        "not json",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {server_and_token[1]}",
    )
    assert resp.status_code == 400


def _queue(server, action="deploy_api", args=None, **kw):
    from datetime import timedelta

    from apps.operations.models import Command

    job = Job.objects.create(action=action, args=args or {})
    return Command.objects.create(
        job=job,
        server=server,
        action=action,
        args=args or {},
        deliver_by=timezone.now() + timedelta(minutes=5),
        **kw,
    )


def test_commands_are_delivered_exactly_once(agent, server_and_token):
    server, _ = server_and_token
    cmd = _queue(server)
    first = agent.poll().json()["commands"]
    assert [c["id"] for c in first] == [str(cmd.pk)]
    assert agent.poll().json()["commands"] == []
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.RUNNING and cmd.delivered_at


def test_expired_commands_are_not_delivered(agent, server_and_token):
    from datetime import timedelta

    cmd = _queue(server_and_token[0])
    cmd.deliver_by = timezone.now() - timedelta(seconds=1)
    cmd.save()
    assert agent.poll().json()["commands"] == []
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.EXPIRED


def test_result_streams_output_then_finalises(agent, server_and_token):
    cmd = _queue(server_and_token[0])
    agent.poll()
    assert agent.result(cmd.pk, status="running", output="step 1\n").status_code == 200
    assert agent.result(cmd.pk, status="succeeded", output="done\n", exit_code=0).status_code == 200
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.SUCCEEDED
    assert cmd.output == "step 1\ndone\n" and cmd.exit_code == 0 and cmd.finished_at


def test_result_output_is_capped_keeping_the_tail(agent, server_and_token, settings):
    settings.MONITOR_COMMAND_OUTPUT_LIMIT = 10
    cmd = _queue(server_and_token[0])
    agent.poll()
    agent.result(cmd.pk, status="failed", output="0123456789ERROR", exit_code=1)
    cmd.refresh_from_db()
    assert cmd.output == "56789ERROR" and cmd.output_truncated


def test_server_cannot_report_on_another_servers_command(client, server_and_token):
    other, _ = make_server("db-01")
    cmd = _queue(other)
    resp = AgentClient(client, server_and_token[1]).result(cmd.pk, status="succeeded", exit_code=0)
    assert resp.status_code == 404
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING


def test_disabled_server_gets_no_commands(agent, server_and_token):
    server, _ = server_and_token
    _queue(server)
    server.is_enabled = False
    server.save()
    assert agent.poll().json()["commands"] == []


def test_heartbeat_query_budget(agent, server_and_token, django_assert_max_num_queries):
    agent.heartbeat()
    # auth, update, sample insert, open alerts, cpu history, claim (savepoints included).
    with django_assert_max_num_queries(12):
        assert agent.heartbeat().status_code == 200


def test_service_down_opens_alert_and_recovery_resolves(agent, server_and_token):
    down = report(
        services=[{"name": "nginx", "kind": "systemd", "state": "down", "status": "failed"}]
    )
    agent.heartbeat(down)
    agent.heartbeat(down)
    alerts = Alert.objects.filter(kind=AlertKind.SERVICE, subject="nginx")
    assert alerts.count() == 1 and alerts.get().is_open
    agent.heartbeat()
    assert not alerts.get().is_open


def test_cpu_alert_needs_sustained_load(agent):
    hot = report(metrics={"cpu_percent": 99.0})
    agent.heartbeat(hot)
    agent.heartbeat(hot)
    assert not Alert.objects.filter(kind=AlertKind.CPU).exists()
    agent.heartbeat(hot)
    assert Alert.objects.get(kind=AlertKind.CPU).severity == "critical"


def test_disk_alert_hysteresis(agent):
    agent.heartbeat(report(metrics={"disk_percent": 92.0}))
    assert Alert.objects.get(kind=AlertKind.DISK).is_open
    agent.heartbeat(report(metrics={"disk_percent": 88.0}))  # below 90 but within 5 % band
    assert Alert.objects.get(kind=AlertKind.DISK).is_open
    agent.heartbeat(report(metrics={"disk_percent": 80.0}))
    assert not Alert.objects.get(kind=AlertKind.DISK).is_open


def test_payload_body_never_echoes_token(agent, server_and_token):
    body = json.dumps(agent.heartbeat().json())
    assert server_and_token[1] not in body
