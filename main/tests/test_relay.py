"""The Redis relay: heartbeats, dispatch, acks, results, cancel/expiry and provisioning,
driven through the real bridge with an in-memory Redis (see conftest.FakeRedis)."""

from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone

from apps.agent_api import relay
from apps.alerts.models import Alert, AlertKind
from apps.fleet.models import Health, MetricSample
from apps.operations import services
from apps.operations.models import Command, CommandStatus

from .conftest import AgentClient, make_server, report

pytestmark = pytest.mark.django_db


def _request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _job(user, server, action="deploy_api", args=None, capture=None):
    if capture is not None:
        with capture(execute=True):
            job, _ = services.create_job(_request(user), action, args or {}, [server])
    else:
        job, _ = services.create_job(_request(user), action, args or {}, [server])
    return job.commands.get()


@pytest.fixture
def deployable(server_and_token, agent):
    agent.heartbeat()  # advertises deploy_api, nginx_error, restartable nginx
    return server_and_token[0]


# ---------------------------------------------------------------- heartbeats


def test_heartbeat_stores_snapshot_and_sample(agent, server_and_token):
    agent.heartbeat(report(host={"address": "10.0.0.7"}))
    server = server_and_token[0]
    server.refresh_from_db()
    assert server.cpu_percent == 12.5 and server.hostname == "web-01"
    assert server.health == Health.ONLINE and server.remote_addr == "10.0.0.7"
    assert server.action_names == {"deploy_api"}
    assert MetricSample.objects.filter(server=server).count() == 1


def test_interval_mismatch_pushes_schedule_to_agent(agent):
    agent.heartbeat(interval=30)
    assert agent.mailbox() == [{"t": "cfg", "report_interval": agent.server.report_interval}]
    agent.heartbeat()  # in sync: nothing more to say
    assert agent.mailbox() == []


def test_bad_payload_is_ignored_and_clamped(agent, server_and_token):
    agent.heartbeat(report(metrics={"cpu_percent": "lots"}))
    assert MetricSample.objects.count() == 0
    agent.heartbeat(report(metrics={"cpu_percent": 250.0}))
    server_and_token[0].refresh_from_db()
    assert server_and_token[0].cpu_percent == 100.0
    agent._send({"t": "res", "id": "not-a-uuid", "status": "succeeded"})  # must not raise
    agent._send({"t": "???"})


def test_disabled_server_heartbeats_are_ignored(agent, server_and_token):
    server = server_and_token[0]
    server.is_enabled = False
    server.save()
    agent.heartbeat()
    assert MetricSample.objects.count() == 0


def test_heartbeat_query_budget(agent, django_assert_max_num_queries):
    agent.heartbeat()
    with django_assert_max_num_queries(12):
        agent.heartbeat()


# ---------------------------------------------------------------- jobs


def test_job_is_dispatched_once_and_acked(
    agent, deployable, operator, django_capture_on_commit_callbacks
):
    cmd = _job(operator, deployable, capture=django_capture_on_commit_callbacks)
    queued = agent.mailbox()
    assert [m["t"] for m in queued] == ["cmd"] and queued[0]["id"] == str(cmd.pk)
    assert queued[0]["exp"] == int(cmd.deliver_by.timestamp())
    assert agent.mailbox() == []  # popped: nobody else can receive it
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING and cmd.dispatched_at
    agent.ack(cmd.pk)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.RUNNING and cmd.delivered_at


def test_result_streams_output_then_finalises(agent, deployable, operator, settings):
    cmd = _job(operator, deployable)
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="running", output="step 1\n")
    agent.result(cmd.pk, status="succeeded", output="done\n", exit_code=0)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.SUCCEEDED and cmd.exit_code == 0
    assert cmd.output == "step 1\ndone\n" and cmd.finished_at


def test_output_is_capped_keeping_the_tail(agent, deployable, operator, settings):
    settings.MONITOR_COMMAND_OUTPUT_LIMIT = 10
    cmd = _job(operator, deployable)
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="failed", output="0123456789ERROR", exit_code=1)
    cmd.refresh_from_db()
    assert cmd.output == "56789ERROR" and cmd.output_truncated


def test_server_cannot_report_on_another_servers_command(fake_relay, agent, operator):
    other, _ = make_server("db-01", capabilities={"actions": [{"name": "deploy_api"}]})
    cmd = _job(operator, other)
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="succeeded", exit_code=0)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING


def test_cancel_pulls_an_undelivered_command_from_the_mailbox(
    agent, deployable, operator, django_capture_on_commit_callbacks
):
    cmd = _job(operator, deployable, capture=django_capture_on_commit_callbacks)
    cmd.refresh_from_db()
    assert services.cancel_command(_request(operator), cmd)
    assert agent.mailbox() == []  # the agent will never see it
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.CANCELLED


def test_cannot_cancel_once_the_agent_popped_it(
    agent, deployable, operator, django_capture_on_commit_callbacks
):
    cmd = _job(operator, deployable, capture=django_capture_on_commit_callbacks)
    agent.mailbox()  # agent BLPOPped it; ack not processed yet
    cmd.refresh_from_db()
    assert not services.cancel_command(_request(operator), cmd)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING


def test_late_output_for_a_lost_command_tells_agent_to_stop(agent, deployable, operator):
    cmd = _job(operator, deployable)
    agent.ack(cmd.pk)
    Command.objects.filter(pk=cmd.pk).update(status=CommandStatus.LOST)
    agent.mailbox()
    agent.result(cmd.pk, status="running", output="still going\n")
    assert agent.mailbox() == [{"t": "cancel", "id": str(cmd.pk)}]


def test_outbox_retries_when_relay_was_down(
    fake_relay, agent, deployable, operator, django_capture_on_commit_callbacks
):
    fake_relay.down = True
    cmd = _job(operator, deployable, capture=django_capture_on_commit_callbacks)
    cmd.refresh_from_db()
    assert cmd.dispatched_at is None  # job still created: the outbox has it
    fake_relay.down = False
    assert services.dispatch_pending() == 1
    assert [m["id"] for m in agent.mailbox()] == [str(cmd.pk)]
    assert services.dispatch_pending() == 0


def test_expiry_removes_queued_command_from_mailbox(
    agent, deployable, operator, django_capture_on_commit_callbacks
):
    cmd = _job(operator, deployable, capture=django_capture_on_commit_callbacks)
    cmd.refresh_from_db()
    services.expire_commands(timezone.now() + timedelta(minutes=10))
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.EXPIRED and agent.mailbox() == []


# ---------------------------------------------------------------- alerts


def test_service_down_opens_alert_and_recovery_resolves(agent):
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


# ---------------------------------------------------------------- provisioning


def test_each_agent_user_is_confined_to_its_own_two_keys(fake_relay, server_and_token):
    server = server_and_token[0]
    url = relay.provision(server)
    rules = fake_relay.users[f"sm-{server.pk}"]
    cmd_key, in_key = relay.keys(server)
    assert {r for r in rules if str(r).startswith("~")} == {f"~{cmd_key}", f"~{in_key}"}
    assert "-@all" in rules and set(relay.AGENT_COMMANDS) <= set(rules)
    assert url.startswith(f"rediss://sm-{server.pk}:tok_") and url.endswith("@relay.test:6379")
    assert "admin-secret" not in url


def test_delete_revokes_relay_user(fake_relay, server_and_token):
    from apps.fleet.services import delete_server

    server = server_and_token[0]
    relay.provision(server)
    delete_server(server)
    assert f"sm-{server.pk}" not in fake_relay.users


def test_agent_client_helper_is_exported():
    assert AgentClient  # used by other test modules


def test_plain_redis_without_gentoken_gets_a_random_password(
    fake_relay, server_and_token, monkeypatch
):
    """Redis/Valkey have no ACL GENTOKEN; provisioning must still work there."""
    import redis

    real = fake_relay.execute_command

    def plain_redis(*args):
        if [str(a).upper() for a in args[:2]] == ["ACL", "GENTOKEN"]:
            raise redis.ResponseError("ERR unknown subcommand 'GENTOKEN'. Try ACL HELP.")
        if [str(a).upper() for a in args[:2]] == ["ACL", "SETUSER"]:
            fake_relay.users[args[2]] = list(args[3:])  # plain Redis takes any >password
            return "OK"
        return real(*args)

    monkeypatch.setattr(fake_relay, "execute_command", plain_redis)
    url = relay.provision(server_and_token[0])
    password = url.split(":")[2].split("@")[0]
    assert password.startswith("smk_") and len(password) > 40
    assert f">{password}" in fake_relay.users[f"sm-{server_and_token[0].pk}"]
