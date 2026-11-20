"""Server tasks (updates, upgrade, cleanup, reboot). Scripts: test_automations.py."""

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.agent_api.schema import clean_report
from apps.operations import services
from apps.operations.models import Job

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
