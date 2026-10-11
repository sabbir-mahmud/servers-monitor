"""4.3: live log follow from the dashboard."""

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.operations import services
from apps.operations.models import Command, CommandStatus

from .conftest import report

pytestmark = pytest.mark.django_db

FOLLOW = {"features": ["log_follow"]}


def follow(client, server, name, flt=""):
    return client.post(reverse("operations:job_create"),
                       {"servers": [server.pk], "action": "tail_follow", "name": name, "filter": flt},
                       HTTP_HX_REQUEST="true")  # fmt: skip


def test_follow_a_log_or_a_service_journal(
    client, operator, agent, django_capture_on_commit_callbacks
):
    agent.heartbeat(report(capabilities=FOLLOW))
    client.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        follow(client, agent.server, "nginx_error", "ERROR")
        follow(client, agent.server, "journal:nginx")
    sent = [m for m in agent.mailbox() if m["t"] == "cmd"]
    assert [m["args"] for m in sent] == [
        {"source": "log", "name": "nginx_error", "filter": "ERROR", "seconds": 600},
        {"source": "journal", "name": "nginx", "filter": "", "seconds": 600},
    ]
    assert Command.objects.first().timeout_seconds == 630
    assert Command.objects.first().job.summary == "follow nginx_error | ERROR"


def test_older_agents_and_undeclared_logs_are_refused(client, operator, agent):
    agent.heartbeat()  # no log_follow feature
    client.force_login(operator)
    assert "too old to follow" in follow(client, agent.server, "nginx_error").content.decode()
    agent.heartbeat(report(capabilities=FOLLOW))
    assert "no systemd service" in follow(client, agent.server, "journal:sshd").content.decode()
    assert not Command.objects.exists()
    with pytest.raises(services.JobError, match="only be followed"):
        services.clean_args("tail_log", {"name": "journal:nginx"})


def test_stop_tells_the_agent_and_is_audited(
    client, operator, agent, django_capture_on_commit_callbacks
):
    agent.heartbeat(report(capabilities=FOLLOW))
    client.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        follow(client, agent.server, "nginx_error")
    command = Command.objects.get()
    agent.mailbox()
    agent.ack(command.pk)
    r = client.post(reverse("operations:command_stop", args=[command.pk]), HTTP_HX_REQUEST="true")
    assert "Stopping" in r.content.decode()
    assert agent.mailbox() == [{"t": "cancel", "id": str(command.pk)}]  # unsigned test server
    assert AuditLog.objects.filter(action="command.stop").exists()
    agent.result(
        command.pk, status="succeeded", output="[agent] stopped from the dashboard\n", exit_code=0
    )
    assert Command.objects.get().status == CommandStatus.SUCCEEDED


def test_only_follows_can_be_stopped(client, operator, agent, django_capture_on_commit_callbacks):
    agent.heartbeat()
    client.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("operations:job_create"),
                    {"servers": [agent.server.pk], "action": "deploy_api"}, HTTP_HX_REQUEST="true")  # fmt: skip
    command = Command.objects.get()
    agent.mailbox()
    agent.ack(command.pk)
    client.post(reverse("operations:command_stop", args=[command.pk]), HTTP_HX_REQUEST="true")
    assert agent.mailbox() == [] and Command.objects.get().status == CommandStatus.RUNNING


def test_viewers_cannot_stop(client, viewer, agent):
    client.force_login(viewer)
    assert (
        client.post(
            reverse("operations:command_stop", args=["0192f0c4-7a1e-7c3e-9c1a-2b3c4d5e6f70"])
        ).status_code
        == 403
    )


def test_server_page_offers_follow_only_to_new_agents(client, operator, agent):
    client.force_login(operator)
    agent.heartbeat()
    page = client.get(reverse("fleet:server_detail", args=[agent.server.pk])).content.decode()
    assert 'value="tail_follow"' not in page
    agent.heartbeat(report(capabilities=FOLLOW))
    page = client.get(reverse("fleet:server_detail", args=[agent.server.pk])).content.decode()
    assert 'value="tail_follow"' in page and 'value="journal:nginx"' in page
