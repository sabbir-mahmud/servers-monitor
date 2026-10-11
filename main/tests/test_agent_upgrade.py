"""3.3: agent upgrades from the dashboard."""

import pytest
from django.urls import reverse

from apps.fleet import agentdist
from apps.operations import services
from apps.operations.models import Command, CommandStatus, Job, JobState

from .conftest import AgentClient, make_server, report

pytestmark = pytest.mark.django_db

OPTED_IN = {"features": ["remote_upgrade"], "actions": [], "logs": [], "shell": False}


@pytest.fixture(autouse=True)
def release(settings):
    settings.AGENT_REF = "v2.2.0"


def old_agent(fake_relay, name, caps=OPTED_IN, signed=True):
    server = make_server(name, **({"signing_salt": "ab" * 16} if signed else {}))[0]
    client = AgentClient(server, fake_relay)
    client.heartbeat(report(agent_version="2.1.0", capabilities=caps))
    client.mailbox()
    return client


def test_upgrade_carries_this_dashboards_tag_and_checksums(
    admin_user, fake_relay, django_capture_on_commit_callbacks
):
    agent = old_agent(fake_relay, "web-01")
    with django_capture_on_commit_callbacks(execute=True):
        job, skipped = services.create_job(
            admin_user, "_upgrade", {"ref": "v0.0.1-evil", "sums": {}}, [agent.server]
        )
    (cmd,) = [m for m in agent.mailbox() if m["t"] == "cmd"]
    assert cmd["action"] == "_upgrade" and cmd["args"]["ref"] == "v2.2.0"  # never from the request
    assert cmd["args"]["sums"] == agentdist.sums() and set(cmd["args"]["sums"]) == set(
        agentdist.FILES
    )
    assert (
        job.summary == "upgrade agent to v2.2.0" and Command.objects.get().timeout_seconds == 1200
    )


def test_only_release_tags_and_only_admins(settings, admin_user, operator, fake_relay):
    agent = old_agent(fake_relay, "web-01")
    with pytest.raises(services.JobError, match="Only Admins"):
        services.create_job(operator, "_upgrade", {}, [agent.server])
    settings.AGENT_REF = "main"
    with pytest.raises(services.JobError, match="release tag"):
        services.create_job(admin_user, "_upgrade", {}, [agent.server])


def test_servers_that_cannot_upgrade_are_skipped_with_the_reason(admin_user, fake_relay):
    ok = old_agent(fake_relay, "ok")
    off = old_agent(fake_relay, "off", caps={"features": []})
    legacy = old_agent(fake_relay, "legacy", signed=False)
    current = make_server("current", signing_salt="cd" * 16)[0]
    AgentClient(current, fake_relay).heartbeat(
        report(agent_version=agentdist.agent_version(), capabilities=OPTED_IN)
    )
    job, skipped = services.create_job(
        admin_user, "_upgrade", {}, [ok.server, off.server, legacy.server, current]
    )
    reasons = {s.name: r for s, r in skipped}
    assert list(job.commands.values_list("server__name", flat=True)) == ["ok"]
    assert "smctl remote-upgrade on" in reasons["off"] and "unsigned" in reasons["legacy"]
    assert reasons["current"].startswith("already on v")


def test_upgrade_all_moves_the_fleet_in_batches(
    client, admin_user, fake_relay, django_capture_on_commit_callbacks
):
    agents = [old_agent(fake_relay, f"web-{i}") for i in range(1, 5)]
    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("fleet:upgrade_all"), {"batch_size": 2})
    job = Job.objects.get()
    assert r["Location"] == reverse("operations:job_detail", args=[job.pk])
    statuses = dict(job.commands.values_list("server__name", "status"))
    assert statuses == {
        "web-1": "pending",
        "web-2": "pending",
        "web-3": "waiting",
        "web-4": "waiting",
    }
    for agent in agents[:2]:
        command = Command.objects.get(server=agent.server)
        agent.ack(command.pk)
        agent.result(command.pk, status="running", output="Upgrade to v2.2.0 requested…\n")
        agent.result(command.pk, status="succeeded", output="Done.\n", exit_code=0)  # from root
    Job.objects.filter(pk=job.pk).update(pause_seconds=0)
    services.advance_rollouts()
    services.advance_rollouts()
    assert set(
        job.commands.filter(status=CommandStatus.PENDING).values_list("server__name", flat=True)
    ) == {"web-3", "web-4"}
    command = Command.objects.get(server=agents[2].server)
    agents[2].ack(command.pk)
    agents[2].result(command.pk, status="failed", output="checksum mismatch\n", exit_code=1)
    agents[3].ack(Command.objects.get(server=agents[3].server).pk)
    agents[3].result(
        Command.objects.get(server=agents[3].server).pk, status="succeeded", output="", exit_code=0
    )
    services.advance_rollouts()
    assert Job.objects.get().state == JobState.HALTED  # web-3 failed: reported, not hidden


def test_upgrade_buttons(client, admin_user, operator, fake_relay):
    agent = old_agent(fake_relay, "web-01")
    client.force_login(admin_user)
    page = client.get(reverse("fleet:server_detail", args=[agent.server.pk])).content.decode()
    assert "Upgrade agent now" in page and "update available" in page
    assert "Upgrade all outdated" in client.get(reverse("system")).content.decode()
    client.force_login(operator)
    assert client.post(reverse("fleet:server_upgrade", args=[agent.server.pk])).status_code == 403


def test_install_commands_verify_the_upgrade_units_too():
    command = agentdist.install_command()
    assert "upath=" in command and "uservice=" in command
