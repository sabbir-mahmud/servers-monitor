"""Git deploys: projects, push webhooks, approval, queueing and results."""

import hashlib
import hmac
import json

import pytest
from django.urls import reverse

from apps.agent_api.schema import clean_report
from apps.audit.models import AuditLog
from apps.deploys import services, webhooks
from apps.deploys.models import Deployment, DeployMode, DeployStatus, Project, Source
from apps.operations import services as ops
from apps.operations.models import Command, CommandStatus

from .conftest import AgentClient, acme, make_server, report

pytestmark = pytest.mark.django_db

SHA = "a" * 40
SHA2 = "b" * 40


def caps(mode="dashboard", sha=""):
    return {"features": ["deploy"], "shell": True,
            "apps": [{"name": "api", "repo": "git@github.com:acme/api.git", "branch": "main",
                      "mode": mode, "timeout": 600, "sha": sha}]}  # fmt: skip


def project(user, servers, **kwargs):
    kwargs.setdefault("steps", "npm ci\n")
    p = Project.objects.create(
        account=acme(), name=kwargs.pop("name", "API"), app="api",
        repo_url="git@github.com:acme/api.git", branch="main", created_by=user,
        webhook_secret=services.new_secret(), **kwargs,
    )  # fmt: skip
    p.servers.set(servers)
    return p


def github(p, body, event="push", delivery="d-1", secret=None):
    raw = json.dumps(body).encode()
    sig = hmac.new((secret or services.secret_of(p)).encode(), raw, hashlib.sha256).hexdigest()
    return {"data": raw, "content_type": "application/json",
            "headers": {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                        "X-Hub-Signature-256": f"sha256={sig}"}}  # fmt: skip


def push_body(sha=SHA, ref="refs/heads/main"):
    return {"ref": ref, "after": sha, "deleted": False,
            "head_commit": {"id": sha, "message": "Fix login\n\nlonger body",
                            "author": {"name": "Ada"}}}  # fmt: skip


def hook(client, p, **kw):
    return client.post(reverse("deploy_webhook", args=[p.pk]), **kw)


# ------------------------------------------------------------------- heartbeat + jobs


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
    script, _ = make_server("script", capabilities=caps(mode="script"))
    none, _ = make_server("none", capabilities={"features": ["deploy"], "apps": []})
    old, _ = make_server("old", capabilities={"features": []})
    job, skipped = ops.create_job(admin_user, "_deploy", {"app": "api", "sha": SHA, "steps": "make\n"},
                                  [ok, script, none, old])  # fmt: skip
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


# ------------------------------------------------------------------------- webhooks


def test_github_push_deploys_automatically(client, admin_user, fake_relay):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server])
    resp = hook(client, p, **github(p, push_body()))
    assert resp.status_code == 202
    dep = Deployment.objects.get()
    assert dep.status == DeployStatus.RUNNING and dep.sha == SHA and dep.source == Source.PUSH
    assert dep.message == "Fix login" and dep.author == "Ada"
    assert dep.job.commands.get().args == {"app": "api", "sha": SHA, "steps": "npm ci\n"}


def test_bad_signature_is_refused(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server])
    assert hook(client, p, **github(p, push_body(), secret="wrong")).status_code == 403
    assert not Deployment.objects.exists()


def test_other_branches_pings_and_replays_do_nothing(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server])
    assert hook(client, p, **github(p, {"zen": "hi"}, event="ping")).status_code == 202
    assert hook(client, p, **github(p, push_body(ref="refs/heads/dev"))).status_code == 202
    assert hook(client, p, **github(p, push_body(sha="0" * 40))).status_code == 202
    assert not Deployment.objects.exists()
    hook(client, p, **github(p, push_body()))
    assert hook(client, p, **github(p, push_body())).status_code == 200  # same delivery id
    assert Deployment.objects.count() == 1


def test_disabled_project_has_no_webhook(client, admin_user):
    p = project(admin_user, [], is_enabled=False)
    assert hook(client, p, **github(p, push_body())).status_code == 404


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


# ------------------------------------------------------------------ approval + queue


def test_approval_projects_wait_for_an_admin(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server], mode=DeployMode.APPROVAL)
    hook(client, p, **github(p, push_body()))
    dep = Deployment.objects.get()
    assert dep.status == DeployStatus.WAITING and dep.job is None

    client.force_login(operator)  # operators can't approve
    url = reverse("deploys:decide", args=[p.pk, dep.pk, "approve"])
    assert client.post(url).status_code == 403

    client.force_login(admin_user)
    client.post(url)
    dep.refresh_from_db()
    assert dep.status == DeployStatus.RUNNING and dep.decided_by == admin_user
    assert AuditLog.objects.filter(action="deploy.approve").exists()


def test_operator_deploy_now_waits_on_approval_projects(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server], mode=DeployMode.APPROVAL)
    client.force_login(operator)
    client.post(reverse("deploys:deploy", args=[p.pk]))
    assert Deployment.objects.get().status == DeployStatus.WAITING
    client.force_login(admin_user)
    client.post(reverse("deploys:deploy", args=[p.pk]))
    assert Deployment.objects.filter(status=DeployStatus.RUNNING).count() == 1


def test_reject(client, admin_user):
    p = project(admin_user, [], mode=DeployMode.APPROVAL)
    dep = services.request_deploy(p, source=Source.PUSH, sha=SHA)
    client.force_login(admin_user)
    client.post(reverse("deploys:decide", args=[p.pk, dep.pk, "reject"]))
    dep.refresh_from_db()
    assert dep.status == DeployStatus.REJECTED and dep.finished_at


def test_pushes_during_a_deploy_coalesce_and_results_settle(admin_user, fake_relay):
    server, _ = make_server("web", capabilities=caps())
    agent = AgentClient(server, fake_relay)
    p = project(admin_user, [server])
    first = services.request_deploy(p, source=Source.PUSH, sha=SHA)
    second = services.request_deploy(p, source=Source.PUSH, sha=SHA2)
    third = services.request_deploy(p, source=Source.PUSH, sha="c" * 40)
    assert first.status == DeployStatus.RUNNING
    assert second.status == third.status == DeployStatus.QUEUED

    cmd = first.job.commands.get()
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="running", output="Fetching…\n")
    assert services.sync() == 0
    agent.result(cmd.pk, status="succeeded", output="Deployed.\n", exit_code=0)
    assert services.sync() == 1

    for d in (first, second, third):
        d.refresh_from_db()
    assert first.status == DeployStatus.SUCCEEDED and first.finished_at
    assert second.status == DeployStatus.SUPERSEDED
    assert third.status == DeployStatus.RUNNING
    Command.objects.filter(job=third.job).update(status=CommandStatus.FAILED, exit_code=1)
    services.sync()
    third.refresh_from_db()
    assert third.status == DeployStatus.FAILED


def test_no_eligible_server_fails_the_deploy(admin_user):
    old, _ = make_server("old", capabilities={"features": []})
    p = project(admin_user, [old])
    dep = services.request_deploy(p, source=Source.MANUAL, user=admin_user)
    assert dep.status == DeployStatus.FAILED and "too old" in dep.error
    assert services.request_deploy(p, source=Source.MANUAL, user=admin_user).status == "failed"


def test_redeploy_an_earlier_commit(client, admin_user, operator):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server])
    old = Deployment.objects.create(project=p, sha=SHA, message="Good one", source=Source.PUSH,
                                    status=DeployStatus.SUCCEEDED)  # fmt: skip
    client.force_login(operator)
    client.post(reverse("deploys:deploy", args=[p.pk]), {"redeploy": str(old.pk)})
    new = Deployment.objects.exclude(pk=old.pk).get()
    assert new.source == Source.ROLLBACK and new.sha == SHA and new.status == DeployStatus.RUNNING


# ---------------------------------------------------------------------------- views


def test_admin_creates_a_project_and_sees_the_setup(client, admin_user):
    server, _ = make_server("web", capabilities=caps())
    client.force_login(admin_user)
    resp = client.post(reverse("deploys:create"), {
        "name": "API", "repo_url": "git@github.com:acme/api.git", "branch": "main", "app": "api",
        "servers": [str(server.pk)], "mode": "auto", "steps": "npm ci\r\nnpm run build",
        "strategy": "all", "batch_size": 1, "stop_on_failure": "on", "is_enabled": "on",
    })  # fmt: skip
    p = Project.objects.get()
    assert resp.status_code == 302 and p.steps == "npm ci\nnpm run build\n"
    assert not p.webhook_secret.startswith(services.secret_of(p)[:6])  # stored encrypted
    page = client.get(reverse("deploys:project", args=[p.pk])).content.decode()
    assert f"/hooks/git/{p.pk}/" in page and services.secret_of(p) in page
    assert "smctl app add api" in page
    assert client.get(reverse("deploys:list")).status_code == 200


def test_operators_see_projects_but_not_the_secret(client, admin_user, operator):
    p = project(admin_user, [])
    client.force_login(operator)
    page = client.get(reverse("deploys:project", args=[p.pk])).content.decode()
    assert services.secret_of(p) not in page
    assert client.get(reverse("deploys:create")).status_code == 403
    assert client.post(reverse("deploys:secret", args=[p.pk])).status_code == 403


def test_rotating_the_secret_breaks_the_old_one(client, admin_user):
    p = project(admin_user, [])
    old = services.secret_of(p)
    client.force_login(admin_user)
    client.post(reverse("deploys:secret", args=[p.pk]))
    p.refresh_from_db()
    assert services.secret_of(p) != old
    assert hook(client, p, **github(p, push_body(), secret=old)).status_code == 403


def test_deployment_page_shows_each_servers_log(client, admin_user, fake_relay):
    server, _ = make_server("web", capabilities=caps())
    p = project(admin_user, [server])
    dep = services.request_deploy(p, source=Source.MANUAL, user=admin_user)
    agent, cmd = AgentClient(server, fake_relay), dep.job.commands.get()
    agent.ack(cmd.pk)
    agent.result(cmd.pk, status="running", output="Building 42 modules\n")
    client.force_login(admin_user)
    page = client.get(reverse("deploys:deployment", args=[p.pk, dep.pk])).content.decode()
    assert "Building 42 modules" in page and "web" in page


def test_projects_are_private_to_their_account(client, admin_user):
    from apps.tenants.models import Account

    other = Account.objects.create(name="Other", slug="other")
    p = Project.objects.create(account=other, name="Theirs", app="api", repo_url="x",
                               webhook_secret=services.new_secret())  # fmt: skip
    client.force_login(admin_user)
    assert client.get(reverse("deploys:project", args=[p.pk])).status_code == 404
    assert "Theirs" not in client.get(reverse("deploys:list")).content.decode()
