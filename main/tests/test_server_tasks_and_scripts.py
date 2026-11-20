"""Server tasks (updates, upgrade, cleanup, reboot) and saved scripts."""

import shlex

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.agent_api.schema import clean_report
from apps.audit.models import AuditLog
from apps.operations import services
from apps.operations.models import Job, Script

from .conftest import make_server, report

pytestmark = pytest.mark.django_db

TASKS_ON = {"features": ["tasks"], "tasks": ["updates", "upgrade", "cleanup", "reboot"],
            "package_manager": "apt", "shell": True}  # fmt: skip


def _request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


# --------------------------------------------------------------------------- server tasks


def test_heartbeat_keeps_only_known_tasks():
    caps = clean_report(report(capabilities={
        "tasks": ["upgrade", "format_disk"], "package_manager": "apt", "reboot_required": True,
    }))["capabilities"]  # fmt: skip
    assert caps["tasks"] == ["upgrade"]
    assert caps["package_manager"] == "apt" and caps["reboot_required"] is True


def test_task_runs_only_where_the_server_turned_it_on(admin_user):
    on, _ = make_server("on", capabilities=TASKS_ON)
    off, _ = make_server("off", capabilities={**TASKS_ON, "tasks": ["updates"]})
    old, _ = make_server("old", capabilities={"features": []})
    job, skipped = services.create_job(_request(admin_user), "_task", {"task": "upgrade"},
                                       [on, off, old])  # fmt: skip
    command = job.commands.get()
    assert command.server == on and command.args == {"task": "upgrade"}
    assert command.timeout_seconds == 3600
    reasons = {s.name: r for s, r in skipped}
    assert "smctl tasks on upgrade" in reasons["off"] and "too old" in reasons["old"]
    assert job.summary == "upgrade packages"


def test_unknown_tasks_are_refused(admin_user):
    on, _ = make_server("on", capabilities=TASKS_ON)
    with pytest.raises(services.JobError, match="Choose a server task"):
        services.create_job(_request(admin_user), "_task", {"task": "rm -rf /"}, [on])


def test_operators_may_check_updates_but_not_reboot(operator):
    on, _ = make_server("on", capabilities=TASKS_ON)
    services.create_job(_request(operator), "_task", {"task": "updates"}, [on])
    with pytest.raises(services.JobError, match="Only Admins"):
        services.create_job(_request(operator), "_task", {"task": "reboot"}, [on])


def test_server_page_task_buttons_post_through_the_job_form(client, admin_user):
    server, _ = make_server("web-01", capabilities={**TASKS_ON, "tasks": ["reboot"],
                                                    "reboot_required": True})  # fmt: skip
    client.force_login(admin_user)
    page = client.get(reverse("fleet:server_detail", args=[server.pk])).content.decode()
    assert "Reboot needed" in page and 'value="_task:reboot"' in page
    assert "sudo smctl tasks on upgrade" in page  # the ones that are off say how
    resp = client.post(reverse("operations:job_create"),
                       {"servers": [server.pk], "action": "_task:reboot"})  # fmt: skip
    assert resp.status_code == 302
    job = Job.objects.get()
    assert (job.action, job.args) == ("_task", {"task": "reboot"})


def test_a_task_can_be_scheduled(client, admin_user):
    server, _ = make_server("web-01", capabilities=TASKS_ON)
    client.force_login(admin_user)
    resp = client.post(reverse("operations:schedule_create"), {
        "sched_name": "Weekly cleanup", "cron": "0 4 * * sun", "action": "_task:cleanup",
        "servers": [server.pk], "is_enabled": "on",
    })  # fmt: skip
    assert resp.status_code == 302, resp.content
    from apps.operations.models import Schedule

    sched = Schedule.objects.get()
    assert (sched.action, sched.args) == ("_task", {"task": "cleanup"})
    edit = client.get(reverse("operations:schedule_update", args=[sched.pk])).content.decode()
    assert 'value="_task:cleanup" selected' in edit


# --------------------------------------------------------------------------- scripts


def test_script_command_quotes_the_body_for_its_interpreter():
    body = "echo 'hi' \"$HOME\"\nexit 3\n"
    bash = Script(name="x", interpreter="bash", body=body).command()
    assert bash.startswith("exec bash -c ") and shlex.split(bash)[3] == body
    assert Script(name="x", interpreter="sh", body=body).command() == body
    assert shlex.split(Script(name="p", interpreter="python3", body="print(1)").command())[:3] == [
        "exec", "python3", "-c"]  # fmt: skip


def test_create_and_run_a_script(client, admin_user):
    on, _ = make_server("on", capabilities={"shell": True})
    off, _ = make_server("off", capabilities={"shell": False})
    client.force_login(admin_user)
    resp = client.post(reverse("operations:script_create"), {
        "name": "Disk report", "description": "df", "interpreter": "bash",
        "body": "set -e\r\ndf -h /\r\n", "then": "run",
    })  # fmt: skip
    script = Script.objects.get()
    assert resp["Location"] == reverse("operations:script_run", args=[script.pk])
    assert script.body == "set -e\ndf -h /\n" and script.created_by == admin_user

    page = client.get(reverse("operations:script_run", args=[script.pk]),
                      {"server": on.pk}).content.decode()  # fmt: skip
    assert "Shell is off" in page and "1 of 2 can run scripts" in page

    resp = client.post(reverse("operations:script_run", args=[script.pk]),
                       {"servers": [on.pk], "note": "weekly"})  # fmt: skip
    job = Job.objects.get()
    assert resp["Location"] == reverse("operations:job_detail", args=[job.pk])
    assert job.action == "shell" and job.args["script"] == "Disk report"
    assert job.args["command"] == script.command() and job.summary == "script Disk report"
    assert AuditLog.objects.filter(action="script.save").exists()

    # A server with shell off can't be smuggled in: the agent opt-in is checked again.
    resp = client.post(reverse("operations:script_run", args=[script.pk]), {"servers": [off.pk]})
    assert "No selected server can run" in resp.content.decode()


def test_scripts_are_for_shell_users_and_their_own_account(client, operator, admin_user):
    from .conftest import make_account

    other = Script.objects.create(account=make_account("Globex"), name="theirs", body="id\n")
    client.force_login(operator)
    assert client.get(reverse("operations:script_list")).status_code == 403
    client.force_login(admin_user)
    assert "theirs" not in client.get(reverse("operations:script_list")).content.decode()
    assert client.get(reverse("operations:script_run", args=[other.pk])).status_code == 404
    assert client.post(reverse("operations:script_delete", args=[other.pk])).status_code == 404


def test_script_names_are_unique_per_account(client, admin_user):
    client.force_login(admin_user)
    data = {"name": "Backup", "interpreter": "sh", "body": "true", "then": "list"}
    client.post(reverse("operations:script_create"), data)
    resp = client.post(reverse("operations:script_create"), data)
    assert resp.status_code == 200 and Script.objects.count() == 1
