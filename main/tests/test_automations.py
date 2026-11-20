"""Automations: actions, scripts and git deploys, run by hand, on a schedule or on a push,
with approval, queueing and per-server output. (Rollout and schedule basics: also
test_rollouts.py.)"""

import hashlib
import hmac
import json
import shlex

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.agent_api.schema import clean_report
from apps.audit.models import AuditLog
from apps.automations import services, webhooks
from apps.automations.models import Automation, Kind, Run, RunStatus, Trigger
from apps.automations.templates import BY_KEY
from apps.operations import services as ops
from apps.operations.models import Command, CommandStatus, Job

from .conftest import AgentClient, acme, make_account, make_server, report

pytestmark = pytest.mark.django_db

SHA = "a" * 40
SHA2 = "b" * 40
TASKS_ON = {"features": ["tasks"], "tasks": ["updates", "upgrade", "cleanup", "reboot"],
            "package_manager": "apt", "shell": True}  # fmt: skip


def caps(mode="dashboard", sha=""):
    return {"features": ["deploy"], "shell": True,
            "apps": [{"name": "api", "repo": "git@github.com:acme/api.git", "branch": "main",
                      "mode": mode, "timeout": 600, "sha": sha}]}  # fmt: skip


def deploy(user, servers, **kw):
    kw.setdefault("steps", "npm ci\n")
    a = Automation.objects.create(
        account=acme(), name=kw.pop("name", "API"), kind=Kind.DEPLOY, app="api",
        repo_url="git@github.com:acme/api.git", branch="main", created_by=user,
        on_push=kw.pop("on_push", True), webhook_secret=services.new_secret(), **kw,
    )  # fmt: skip
    a.servers.set(servers)
    return a


def script(user=None, servers=(), **kw):
    a = Automation.objects.create(account=kw.pop("account", None) or acme(),
                                  name=kw.pop("name", "Disk report"), kind=Kind.SCRIPT,
                                  body=kw.pop("body", "df -h /\n"), created_by=user, **kw)  # fmt: skip
    a.servers.set(servers)
    return a


def form(**data):
    base = {"kind": "action", "interpreter": "bash", "is_enabled": "on"}
    return {**base, **data}


def github(a, body, event="push", delivery="d-1", secret=None):
    raw = json.dumps(body).encode()
    sig = hmac.new((secret or services.secret_of(a)).encode(), raw, hashlib.sha256).hexdigest()
    return {"data": raw, "content_type": "application/json",
            "headers": {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                        "X-Hub-Signature-256": f"sha256={sig}"}}  # fmt: skip


def push_body(sha=SHA, ref="refs/heads/main"):
    return {"ref": ref, "after": sha, "deleted": False,
            "head_commit": {"id": sha, "message": "Fix login\n\nlonger body",
                            "author": {"name": "Ada"}}}  # fmt: skip


def hook(client, a, **kw):
    return client.post(reverse("automation_webhook", args=[a.pk]), **kw)


# ============================================================================ the form


def test_one_form_saves_each_kind(client, admin_user):
    server, _ = make_server("web", capabilities={**TASKS_ON, "actions": [{"name": "deploy_api"}]})
    client.force_login(admin_user)
    url = reverse("automations:create")
    r = client.post(url, form(name="Weekly cleanup", action="_task:cleanup", cron="0 4 * * sun",
                              servers=[server.pk]))  # fmt: skip
    assert r.status_code == 302, r.context["form"].errors
    r = client.post(url, form(name="Disk", kind="script", body="set -e\r\ndf -h /\r\n"))
    assert r.status_code == 302, r.context["form"].errors
    r = client.post(url, form(name="API", kind="deploy", repo_url="git@github.com:acme/api.git",
                              branch="main", app="api", on_push="on", servers=[server.pk],
                              steps="npm ci"))  # fmt: skip
    assert r.status_code == 302, r.context["form"].errors

    cleanup, disk, api = (Automation.objects.get(name=n) for n in ("Weekly cleanup", "Disk", "API"))
    assert (cleanup.action, cleanup.args) == ("_task", {"task": "cleanup"})
    assert cleanup.next_run_at and cleanup.body == "" and not cleanup.on_push
    assert disk.body == "set -e\ndf -h /\n" and disk.action == "" and disk.next_run_at is None
    assert api.on_push and api.steps == "npm ci\n" and api.webhook_secret
    assert not api.webhook_secret.startswith(services.secret_of(api)[:6])  # stored encrypted
    edit = client.get(reverse("automations:update", args=[cleanup.pk])).content.decode()
    assert 'value="_task:cleanup" selected' in edit
    assert AuditLog.objects.filter(action="automation.save").count() == 3


@pytest.mark.parametrize("data, error", [
    (form(name="x", action=""), "Choose what to run"),
    (form(name="x", kind="script", body="  "), "Write the script"),
    (form(name="x", kind="deploy"), "Required for a git deploy"),
    (form(name="x", action="deploy_api", cron="0 3 * * *"), "Automatic runs need servers"),
    (form(name="x", action="deploy_api", cron="61 * * * *"), "minute"),
])  # fmt: skip
def test_form_errors(client, admin_user, data, error):
    client.force_login(admin_user)
    resp = client.post(reverse("automations:create"), data)
    assert resp.status_code == 200 and error in resp.content.decode()


def test_names_are_unique_per_account(client, admin_user):
    client.force_login(admin_user)
    data = form(name="Backup", kind="script", body="true")
    client.post(reverse("automations:create"), data)
    resp = client.post(reverse("automations:create"), data)
    assert resp.status_code == 200 and Automation.objects.count() == 1


def test_operators_run_but_dont_edit_and_never_touch_shell(client, admin_user, operator):
    server, _ = make_server(
        "web", capabilities={"shell": True, "actions": [{"name": "deploy_api"}]}
    )
    act = Automation.objects.create(account=acme(), name="Deploy", kind="action",
                                    action="deploy_api", created_by=admin_user)  # fmt: skip
    act.servers.set([server])
    scr = script(admin_user, [server])
    client.force_login(operator)
    assert client.get(reverse("automations:list")).status_code == 200
    assert client.get(reverse("automations:create")).status_code == 403
    assert client.get(reverse("automations:update", args=[act.pk])).status_code == 403
    assert client.post(reverse("automations:run_now", args=[scr.pk])).status_code == 403
    client.post(reverse("automations:run_now", args=[act.pk]))
    assert Run.objects.get().status == RunStatus.RUNNING


# ========================================================================== scripts


def test_script_command_quotes_the_body_for_its_interpreter():
    body = "echo 'hi' \"$HOME\"\nexit 3\n"
    bash = Automation(name="x", kind="script", interpreter="bash", body=body).script_command()
    assert bash.startswith("exec bash -c ") and shlex.split(bash)[3] == body
    assert Automation(name="x", kind="script", interpreter="sh", body=body).script_command() == body
    py = Automation(name="p", kind="script", interpreter="python3", body="print(1)")
    assert shlex.split(py.script_command())[:3] == ["exec", "python3", "-c"]


def test_run_a_script_on_chosen_servers(client, admin_user):
    on, _ = make_server("on", capabilities={"shell": True})
    off, _ = make_server("off", capabilities={"shell": False})
    a = script(admin_user)
    client.force_login(admin_user)
    page = client.get(reverse("automations:run_now", args=[a.pk]), {"server": on.pk})
    assert "shell off" in page.content.decode()
    resp = client.post(reverse("automations:run_now", args=[a.pk]),
                       {"servers": [on.pk], "note": "weekly"})  # fmt: skip
    run = Run.objects.get()
    assert resp["Location"] == reverse("automations:run", args=[a.pk, run.pk])
    job = run.job
    assert job.action == "shell" and job.args["script"] == "Disk report"
    assert job.args["command"] == a.script_command() and job.automation == a
    assert job.note == "weekly" and list(job.commands.values_list("server__name", flat=True)) == [
        "on"
    ]

    # A server with shell off can't be smuggled in: the agent opt-in is checked again.
    Command.objects.filter(job=job).update(status=CommandStatus.SUCCEEDED, exit_code=0)
    services.sync()
    client.post(reverse("automations:run_now", args=[a.pk]), {"servers": [off.pk]})
    failed = Run.objects.exclude(pk=run.pk).get()
    assert failed.status == RunStatus.FAILED and "No selected server can run" in failed.error


def test_a_script_with_no_saved_servers_asks_for_some(client, admin_user):
    a = script(admin_user)
    client.force_login(admin_user)
    resp = client.post(reverse("automations:run_now", args=[a.pk]), {"quick": "1"})
    assert resp["Location"] == reverse("automations:run_now", args=[a.pk])
    assert not Run.objects.exists()


def test_automations_belong_to_their_account(client, admin_user):
    other = script(account=make_account("Globex"), name="theirs")
    client.force_login(admin_user)
    assert "theirs" not in client.get(reverse("automations:list")).content.decode()
    for name in ("detail", "run_now", "update", "delete"):
        assert client.get(reverse(f"automations:{name}", args=[other.pk])).status_code == 404


# ======================================================================== schedules


def test_scheduled_runs_wait_for_approval_when_asked(admin_user):
    server, _ = make_server("web", capabilities={"actions": [{"name": "deploy_api"}]})
    a = Automation.objects.create(account=acme(), name="Nightly", kind="action",
                                  action="deploy_api", cron="0 3 * * *", require_approval=True,
                                  created_by=admin_user, next_run_at=timezone.now())  # fmt: skip
    a.servers.set([server])
    services.run_due()
    run = Run.objects.get()
    assert run.trigger == Trigger.SCHEDULE and run.status == RunStatus.WAITING
    assert not Job.objects.exists()


def test_a_slow_run_doesnt_pile_up_scheduled_ones(admin_user):
    server, _ = make_server("web", capabilities={"actions": [{"name": "deploy_api"}]})
    a = Automation.objects.create(account=acme(), name="Nightly", kind="action",
                                  action="deploy_api", cron="*/5 * * * *", created_by=admin_user)  # fmt: skip
    a.servers.set([server])
    first = services.request_run(a, trigger=Trigger.SCHEDULE)
    second = services.request_run(a, trigger=Trigger.SCHEDULE)
    third = services.request_run(a, trigger=Trigger.SCHEDULE)
    manual = services.request_run(a, trigger=Trigger.MANUAL, user=admin_user)
    assert first.status == RunStatus.RUNNING
    Command.objects.filter(job=first.job).update(status=CommandStatus.SUCCEEDED, exit_code=0)
    services.sync()
    for r in (second, third, manual):
        r.refresh_from_db()
    # The two queued scheduled runs collapse into the newest; the manual one keeps its place.
    assert second.status == RunStatus.SKIPPED
    assert {third.status, manual.status} == {RunStatus.RUNNING, RunStatus.QUEUED}


# ========================================================================== deploys


def test_heartbeat_reports_apps_and_commits():
    clean = clean_report(report(capabilities={"apps": [
        {"name": "api", "mode": "dashboard", "sha": SHA, "timeout": 99999999},
        {"name": "x", "sha": "not-a-sha"}, "junk",
    ]}))["capabilities"]  # fmt: skip
    assert clean["apps"][0]["sha"] == SHA and clean["apps"][0]["mode"] == "dashboard"
    assert clean["apps"][0]["timeout"] == 86400
    assert clean["apps"][1]["sha"] == "" and clean["apps"][1]["mode"] == "script"
    assert len(clean["apps"]) == 2


def test_deploy_command_goes_only_where_the_app_is(admin_user):
    ok, _ = make_server("ok", capabilities=caps())
    scr, _ = make_server("script", capabilities=caps(mode="script"))
    none, _ = make_server("none", capabilities={"features": ["deploy"], "apps": []})
    old, _ = make_server("old", capabilities={"features": []})
    job, skipped = ops.create_job(admin_user, "_deploy", {"app": "api", "sha": SHA, "steps": "make\n"},
                                  [ok, scr, none, old])  # fmt: skip
    cmd = job.commands.get()
    assert cmd.server == ok and cmd.args == {"app": "api", "sha": SHA, "steps": "make\n"}
    assert cmd.timeout_seconds == 600
    reasons = {s.name: r for s, r in skipped}
    assert "own deploy script" in reasons["script"]
    assert "no app 'api'" in reasons["none"] and "too old" in reasons["old"]
    assert job.summary == "deploy api aaaaaaa"


@pytest.mark.parametrize("args, error", [
    ({"app": "-rf", "sha": SHA}, "Name the app"),
    ({"app": "api", "sha": "abc"}, "40-character"),
    ({"app": "api", "sha": SHA, "steps": "x" * 20001}, "limited"),
])  # fmt: skip
def test_deploy_args_are_checked(admin_user, args, error):
    ok, _ = make_server("ok", capabilities=caps())
    with pytest.raises(ops.JobError, match=error):
        ops.create_job(admin_user, "_deploy", args, [ok])


def test_viewers_cannot_deploy(viewer):
    ok, _ = make_server("ok", capabilities=caps())
    with pytest.raises(ops.JobError, match="permission to deploy"):
        ops.create_job(viewer, "_deploy", {"app": "api"}, [ok])


def test_github_push_deploys_automatically(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server])
    resp = hook(client, a, **github(a, push_body()))
    assert resp.status_code == 202
    run = Run.objects.get()
    assert run.status == RunStatus.RUNNING and run.sha == SHA and run.trigger == Trigger.PUSH
    assert run.message == "Fix login" and run.author == "Ada"
    assert run.job.commands.get().args == {"app": "api", "sha": SHA, "steps": "npm ci\n"}
    assert run.job.automation == a


def test_bad_signature_is_refused(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server])
    assert hook(client, a, **github(a, push_body(), secret="wrong")).status_code == 403
    assert not Run.objects.exists()


def test_other_branches_pings_and_replays_do_nothing(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server])
    assert hook(client, a, **github(a, {"zen": "hi"}, event="ping")).status_code == 202
    assert hook(client, a, **github(a, push_body(ref="refs/heads/dev"))).status_code == 202
    assert hook(client, a, **github(a, push_body(sha="0" * 40))).status_code == 202
    assert not Run.objects.exists()
    hook(client, a, **github(a, push_body()))
    assert hook(client, a, **github(a, push_body())).status_code == 200  # same delivery id
    assert Run.objects.count() == 1


def test_no_webhook_unless_on_push_and_enabled(client, admin_user):
    off = deploy(admin_user, [], is_enabled=False)
    manual = deploy(admin_user, [], name="manual", on_push=False)
    assert hook(client, off, **github(off, push_body())).status_code == 404
    assert hook(client, manual, **github(manual, push_body())).status_code == 404


def test_gitlab_gitea_and_bitbucket_pushes_parse():
    secret = "s3cret"
    gl = webhooks.parse({"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": secret},
                        json.dumps({"ref": "refs/heads/main", "checkout_sha": SHA,
                                    "user_name": "Bo", "commits": [{"id": SHA, "message": "m"}]}).encode(),
                        secret)  # fmt: skip
    assert (gl.branch, gl.sha, gl.author, gl.message) == ("main", SHA, "Bo", "m")
    with pytest.raises(webhooks.WebhookError):
        webhooks.parse({"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "x"}, b"{}", secret)

    body = json.dumps(push_body()).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    gt = webhooks.parse({"X-Gitea-Event": "push", "X-GitHub-Event": "push",
                         "X-Gitea-Signature": sig}, body, secret)  # fmt: skip
    assert gt.provider == "gitea" and gt.sha == SHA

    bb_body = json.dumps({"push": {"changes": [{"new": {
        "type": "branch", "name": "main",
        "target": {"hash": SHA2, "message": "Ship\n", "author": {"raw": "Cy <c@x>"}}}}]}}).encode()  # fmt: skip
    sig = hmac.new(secret.encode(), bb_body, hashlib.sha256).hexdigest()
    bb = webhooks.parse({"X-Event-Key": "repo:push", "X-Hub-Signature": f"sha256={sig}"},
                        bb_body, secret)  # fmt: skip
    assert (bb.branch, bb.sha, bb.message, bb.author) == ("main", SHA2, "Ship", "Cy <c@x>")


def test_approval_waits_for_an_admin(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server], require_approval=True)
    hook(client, a, **github(a, push_body()))
    run = Run.objects.get()
    assert run.status == RunStatus.WAITING and run.job is None

    client.force_login(operator)  # operators can't approve
    url = reverse("automations:decide", args=[a.pk, run.pk, "approve"])
    assert client.post(url).status_code == 403
    activity = client.get(reverse("operations:job_list")).content.decode()
    assert "Waiting for approval" in activity and "Fix login" in activity

    client.force_login(admin_user)
    client.post(url)
    run.refresh_from_db()
    assert run.status == RunStatus.RUNNING and run.decided_by == admin_user
    assert AuditLog.objects.filter(action="automation.approve").exists()


def test_operator_run_now_waits_on_approval_automations(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server], require_approval=True)
    client.force_login(operator)
    client.post(reverse("automations:run_now", args=[a.pk]), {"quick": "1"})
    assert Run.objects.get().status == RunStatus.WAITING
    client.force_login(admin_user)
    client.post(reverse("automations:run_now", args=[a.pk]), {"quick": "1"})
    assert Run.objects.filter(status=RunStatus.RUNNING).count() == 1


def test_reject(client, admin_user):
    a = deploy(admin_user, [], require_approval=True)
    run = services.request_run(a, trigger=Trigger.PUSH, sha=SHA)
    client.force_login(admin_user)
    client.post(reverse("automations:decide", args=[a.pk, run.pk, "reject"]))
    run.refresh_from_db()
    assert run.status == RunStatus.REJECTED and run.finished_at


def test_pushes_during_a_deploy_coalesce_and_results_settle(admin_user, fake_relay):
    server, _ = make_server("web", capabilities=caps())
    agent = AgentClient(server, fake_relay)
    a = deploy(admin_user, [server])
    first = services.request_run(a, trigger=Trigger.PUSH, sha=SHA)
    second = services.request_run(a, trigger=Trigger.PUSH, sha=SHA2)
    third = services.request_run(a, trigger=Trigger.PUSH, sha="c" * 40)
    assert first.status == RunStatus.RUNNING
    assert second.status == third.status == RunStatus.QUEUED

    cmd = first.job.commands.get()
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="running", output="Fetching…\n")
    assert services.sync() == 0
    agent.result(cmd.pk, status="succeeded", output="Deployed.\n", exit_code=0)
    assert services.sync() == 1

    for r in (first, second, third):
        r.refresh_from_db()
    assert first.status == RunStatus.SUCCEEDED and first.finished_at
    assert second.status == RunStatus.SKIPPED
    assert third.status == RunStatus.RUNNING
    Command.objects.filter(job=third.job).update(status=CommandStatus.FAILED, exit_code=1)
    services.sync()
    third.refresh_from_db()
    assert third.status == RunStatus.FAILED


def test_no_eligible_server_fails_the_run(admin_user):
    old, _ = make_server("old", capabilities={"features": []})
    a = deploy(admin_user, [old])
    run = services.request_run(a, trigger=Trigger.MANUAL, user=admin_user)
    assert run.status == RunStatus.FAILED and "too old" in run.error
    assert services.request_run(a, trigger=Trigger.MANUAL, user=admin_user).status == "failed"


def test_redeploy_an_earlier_commit(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server])
    old = Run.objects.create(automation=a, sha=SHA, message="Good one", trigger=Trigger.PUSH,
                             status=RunStatus.SUCCEEDED)  # fmt: skip
    client.force_login(operator)
    client.post(
        reverse("automations:run_now", args=[a.pk]), {"redeploy": str(old.pk), "quick": "1"}
    )
    new = Run.objects.exclude(pk=old.pk).get()
    assert new.trigger == Trigger.REDEPLOY and new.sha == SHA and new.status == RunStatus.RUNNING


def test_deploy_page_shows_setup_to_admins_only(client, admin_user, operator):
    a = deploy(admin_user, [])
    client.force_login(admin_user)
    page = client.get(reverse("automations:detail", args=[a.pk])).content.decode()
    assert f"/hooks/git/{a.pk}/" in page and services.secret_of(a) in page
    assert "smctl app add api" in page
    client.force_login(operator)
    page = client.get(reverse("automations:detail", args=[a.pk])).content.decode()
    assert services.secret_of(a) not in page
    assert client.post(reverse("automations:secret", args=[a.pk])).status_code == 403


def test_rotating_the_secret_breaks_the_old_one(client, admin_user):
    a = deploy(admin_user, [])
    old = services.secret_of(a)
    client.force_login(admin_user)
    client.post(reverse("automations:secret", args=[a.pk]))
    a.refresh_from_db()
    assert services.secret_of(a) != old
    assert hook(client, a, **github(a, push_body(), secret=old)).status_code == 403


def test_run_page_shows_each_servers_output(client, admin_user, fake_relay):
    server, _ = make_server("web", capabilities=caps())
    a = deploy(admin_user, [server])
    run = services.request_run(a, trigger=Trigger.MANUAL, user=admin_user)
    agent, cmd = AgentClient(server, fake_relay), run.job.commands.get()
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="running", output="Building 42 modules\n")
    client.force_login(admin_user)
    page = client.get(reverse("automations:run", args=[a.pk, run.pk])).content.decode()
    assert "Building 42 modules" in page and "web" in page


# ===================================================================== pages + links


def test_list_filters_and_activity_labels(client, admin_user):
    server, _ = make_server("web", capabilities={**caps(), "actions": [{"name": "deploy_api"}]})
    deploy(admin_user, [server])
    script(admin_user, name="Disk")
    act = Automation.objects.create(account=acme(), name="Nightly", kind="action",
                                    action="deploy_api", cron="0 3 * * *", created_by=admin_user)  # fmt: skip
    act.servers.set([server])
    services.request_run(act, trigger=Trigger.MANUAL, user=admin_user)
    client.force_login(admin_user)
    page = client.get(reverse("automations:list")).content.decode()
    assert all(n in page for n in ("API", "Disk", "Nightly", "Daily 03:00"))
    page = client.get(reverse("automations:list"), {"kind": "script"}).content.decode()
    assert "Disk" in page and "Nightly" not in page
    page = client.get(reverse("automations:list"), {"when": "push"}).content.decode()
    assert "API" in page and "Disk" not in page
    activity = client.get(reverse("operations:job_list")).content.decode()
    assert "Nightly" in activity and reverse("automations:detail", args=[act.pk]) in activity


def test_old_pages_lead_to_automations(client, admin_user):
    client.force_login(admin_user)
    assert client.get("/jobs/scripts/")["Location"] == "/automations/?kind=script"
    assert client.get("/jobs/schedules/new/")["Location"] == "/automations/?when=scheduled"
    assert client.get("/deploys/")["Location"] == "/automations/?kind=deploy"


def test_server_page_lists_automations_that_run_there(client, admin_user):
    server, _ = make_server(
        "web", capabilities={"shell": True, "actions": [{"name": "deploy_api"}]}
    )
    elsewhere, _ = make_server("db")
    here = Automation.objects.create(
        account=acme(), name="Here", kind="action", action="deploy_api"
    )
    here.servers.set([server])
    there = Automation.objects.create(
        account=acme(), name="There", kind="action", action="deploy_api"
    )
    there.servers.set([elsewhere])
    script(admin_user, name="Anywhere")
    client.force_login(admin_user)
    page = client.get(reverse("fleet:server_detail", args=[server.pk])).content.decode()
    assert "Here" in page and "Anywhere" in page and "There" not in page
    assert f"{reverse('automations:run_now', args=[here.pk])}?server={server.pk}" in page


# ===================================================================== migration


def test_schedules_and_scripts_migrate_into_automations(transactional_db):
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("operations", "0009_scripts"), ("automations", None)])
    executor = MigrationExecutor(connection)
    apps = executor.loader.project_state([("operations", "0009_scripts")]).apps
    Account = apps.get_model("tenants", "Account")
    Schedule = apps.get_model("operations", "Schedule")
    Script = apps.get_model("operations", "Script")
    OldJob = apps.get_model("operations", "Job")
    acct = Account.objects.create(name="Old Co")
    sched = Schedule.objects.create(account=acct, name="Backup", action="deploy_api",
                                    cron="0 3 * * *", args={"x": 1}, batch_size=2)  # fmt: skip
    Script.objects.create(account=acct, name="Backup", interpreter="sh", body="true\n")
    job = OldJob.objects.create(account=acct, action="deploy_api", schedule=sched)

    call_command("migrate", verbosity=0)
    sched_a = Automation.objects.get(pk=sched.pk)
    assert (sched_a.kind, sched_a.action, sched_a.cron, sched_a.args, sched_a.batch_size) == (
        "action", "deploy_api", "0 3 * * *", {"x": 1}, 2)  # fmt: skip
    script_a = Automation.objects.get(kind="script")
    assert script_a.name == "Backup (script)" and script_a.body == "true\n"
    assert Job.objects.get(pk=job.pk).automation_id == sched.pk


def test_search_and_by_hand_filter(client, admin_user):
    script(admin_user, name="Disk report")
    Automation.objects.create(account=acme(), name="Nightly", kind="action", action="deploy_api",
                              cron="0 3 * * *")  # fmt: skip
    client.force_login(admin_user)
    page = client.get(reverse("automations:list"), {"q": "disk"}).content.decode()
    assert "Disk report" in page and "Nightly" not in page
    page = client.get(reverse("automations:list"), {"when": "manual"}).content.decode()
    assert "Disk report" in page and "Nightly" not in page


def test_page_shows_health_and_failures_first(client, admin_user):
    from datetime import timedelta

    a = script(admin_user)
    now = timezone.now()
    for i, status in enumerate(["succeeded", "succeeded", "succeeded", "failed"]):
        Run.objects.create(automation=a, trigger="manual", status=status,
                           started_at=now - timedelta(minutes=10 * i + 2),
                           finished_at=now - timedelta(minutes=10 * i + 1))  # fmt: skip
    client.force_login(admin_user)
    page = client.get(reverse("automations:detail", args=[a.pk])).content.decode()
    assert "75%" in page and "of the last 4 finished" in page and "1m" in page

    ok, _ = make_server("a-ok", capabilities={"shell": True})
    bad, _ = make_server("z-bad", capabilities={"shell": True})
    run = services.request_run(a, trigger=Trigger.MANUAL, user=admin_user, servers=[ok, bad])
    Command.objects.filter(job=run.job, server=bad).update(status=CommandStatus.FAILED, exit_code=2)
    page = client.get(reverse("automations:run", args=[a.pk, run.pk])).content.decode()
    assert page.index("z-bad") < page.index("a-ok")  # the failure is what you came to read


# ---------------------------------------------------------------------------- templates


def test_new_opens_the_template_gallery(client, admin_user):
    from apps.automations.templates import TEMPLATES

    client.force_login(admin_user)
    page = client.get(reverse("automations:create")).content.decode()
    assert all(t.title.replace("'", "&#x27;") in page for t in TEMPLATES)
    assert "From scratch" in page
    page = client.get(reverse("automations:create"), {"kind": "script"}).content.decode()
    assert 'name="body"' in page and "From scratch" not in page


def test_template_fills_the_form(client, admin_user):
    client.force_login(admin_user)
    page = client.get(
        reverse("automations:create"), {"template": "postgres-backup"}
    ).content.decode()
    assert 'value="PostgreSQL backup"' in page and "pg_dump" in page and 'value="0 2 * * *"' in page
    assert 'From the "PostgreSQL backup" template' in page

    script(admin_user, name="PostgreSQL backup")
    page = client.get(
        reverse("automations:create"), {"template": "postgres-backup"}
    ).content.decode()
    assert 'value="PostgreSQL backup 2"' in page  # never collides with an existing name

    page = client.get(reverse("automations:create"), {"template": "nope"}).content.decode()
    assert "From the" not in page  # unknown keys fall back to a blank form


@pytest.mark.parametrize("key", list(BY_KEY))
def test_every_template_saves_as_is(client, admin_user, key):
    """A template must be valid out of the box: pick servers, save, done."""
    t = BY_KEY[key]
    server, _ = make_server("web-01", capabilities={**TASKS_ON, "shell": True})
    client.force_login(admin_user)
    data = form(kind=t.kind, name=t.title, servers=[server.pk])
    data.update({k: ("on" if v is True else v) for k, v in t.initial.items() if v is not False})
    if t.kind == "deploy":
        data["repo_url"] = "git@github.com:acme/app.git"
    r = client.post(reverse("automations:create") + f"?template={key}", data)
    assert r.status_code == 302, r.context["form"].errors
    a = Automation.objects.get(name=t.title)
    assert a.has_targets and a.cron == t.initial.get("cron", "")
    assert a.require_approval == bool(t.initial.get("require_approval"))


def test_script_templates_are_for_shell_users_only():
    from apps.automations import templates

    class NoShell:
        def has_perm(self, perm):
            return perm != "operations.run_shell"

    kinds = {t.kind for t in templates.available(NoShell())}
    assert "script" not in kinds and {"action", "deploy"} <= kinds
