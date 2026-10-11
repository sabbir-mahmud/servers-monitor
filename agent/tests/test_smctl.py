import sys
import tomllib
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
import server_monitor_agent as sma  # noqa: E402
import smctl  # noqa: E402

BASE = {
    "main": {"url": "https://monitor.test", "token": "smk_test", "verify_tls": True},
    "agent": {"report_interval": 30, "disk_mounts": []},
    "logs": {},
    "shell": {"enabled": False},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """smctl pointed at a temp config, validating with the real agent, without root."""
    conf = tmp_path / "agent.toml"
    conf.write_text(smctl.dump_toml({k: dict(v) for k, v in BASE.items()}))
    monkeypatch.setattr(smctl, "CONF", conf)
    monkeypatch.setattr(smctl, "SUDOERS", tmp_path / "sudoers")
    monkeypatch.setattr(smctl, "AGENT_PY", AGENT_DIR / "server_monitor_agent.py")
    monkeypatch.setattr(smctl, "AGENT_PYTHON", Path(sys.executable))
    monkeypatch.setattr(smctl, "need_root", lambda: None)
    monkeypatch.setattr(smctl, "restart_agent", lambda: None)
    monkeypatch.setattr(smctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(smctl.os, "chown", lambda *a: None)
    return conf


def read(conf):
    return tomllib.loads(conf.read_text())


def test_dump_round_trips_through_the_agent(tmp_path):
    data = {
        **BASE,
        "services": {"api": {"kind": "systemd", "unit": "gunicorn-api", "restartable": True}},
        "actions": {
            "deploy": {
                "command": ["/opt/deploy.sh", 'quote "x"', "ünï"],
                "timeout": 900,
                "env": {"DJANGO_SETTINGS_MODULE": "config.settings"},
            }
        },
        "logs": {"app": "/var/log/app.log"},
    }
    path = tmp_path / "a.toml"
    path.write_text(smctl.dump_toml(data))
    assert tomllib.loads(path.read_text()) == data
    cfg = sma.load_config(path)
    assert cfg.services["api"].restartable and cfg.actions["deploy"].command[1] == 'quote "x"'


def test_service_log_action_flow(env, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("hi\n")
    assert smctl.main(["service", "add", "api", "--unit", "gunicorn-api", "--restartable"]) == 0
    assert smctl.main(["log", "add", "app", str(log)]) == 0
    assert (
        smctl.main(["action", "add", "deploy", "-d", "Ship it", "--", "/opt/deploy.sh", "prod"])
        == 0
    )
    data = read(env)
    assert data["services"]["api"] == {
        "kind": "systemd",
        "unit": "gunicorn-api",
        "restartable": True,
    }
    assert data["logs"]["app"] == str(log)
    assert data["actions"]["deploy"]["command"] == ["/opt/deploy.sh", "prod"]
    # The restartable unit got an exact sudo rule.
    assert "NOPASSWD: /usr/bin/systemctl restart gunicorn-api" in (tmp_path / "sudoers").read_text()

    assert smctl.main(["service", "rm", "api"]) == 0
    assert "api" not in read(env).get("services", {})
    assert not (tmp_path / "sudoers").exists()  # no rules left, file removed


def test_sudo_action_gets_exact_rule_and_rejects_unsafe_args(env, tmp_path):
    assert (
        smctl.main(
            ["action", "add", "upgrade", "--sudo", "--", "/usr/bin/apt-get", "-y", "upgrade"]
        )
        == 0
    )
    assert read(env)["actions"]["upgrade"]["command"][:2] == ["sudo", "-n"]
    assert "NOPASSWD: /usr/bin/apt-get -y upgrade" in (tmp_path / "sudoers").read_text()
    # Sudoers wildcards/separators would widen the rule, so they're refused.
    assert smctl.main(["action", "add", "bad", "--sudo", "--", "/bin/rm", "-rf", "/tmp/*"]) == 1
    assert "bad" not in read(env)["actions"]


def test_reserved_and_invalid_names_rejected(env):
    assert smctl.main(["action", "add", "shell", "--", "/bin/true"]) == 1
    assert smctl.main(["service", "add", "bad name"]) == 1
    assert smctl.main(["log", "add", "../x", "/var/log/syslog"]) == 1


def test_agent_validation_blocks_bad_config(env, monkeypatch):
    before = env.read_text()
    # Bypass smctl's own checks to prove the agent is the final gate.
    data = read(env)
    data["services"] = {"x": {"kind": "telepathy"}}
    with pytest.raises(smctl.Fail, match="kind must be one of"):
        smctl.save(data)
    assert env.read_text() == before


def test_token_command(env):
    assert smctl.main(["token", "not-a-token"]) == 1
    assert smctl.main(["token", "smk_new"]) == 0
    assert read(env)["main"]["token"] == "smk_new"


def test_detect_systemd_known_and_custom_units():
    def runner(argv, **_):
        if "list-units" in argv:
            return 0, (
                "nginx.service loaded active running nginx\n"
                "ssh.service loaded active running OpenSSH\n"
                "myapp.service loaded active running My app\n"
                "server-monitor-agent.service loaded active running agent\n"
                "polkit.service loaded active running polkit\n"
            )
        return 0, (
            "Id=nginx.service\nFragmentPath=/lib/systemd/system/nginx.service\n\n"
            "Id=myapp.service\nFragmentPath=/etc/systemd/system/myapp.service\n\n"
            "Id=polkit.service\nFragmentPath=/lib/systemd/system/polkit.service\n"
        )

    found = {f.name: f for f in smctl.detect_systemd(runner)}
    assert set(found) == {"nginx", "myapp"}
    assert found["nginx"].body["version_command"] == ["nginx", "-v"]
    assert found["myapp"].note == "custom unit"


def test_detect_logs_only_existing():
    names = {f.name for f in smctl.detect_logs(exists=lambda p: p.startswith("/var/log/nginx"))}
    assert names == {"nginx_error", "nginx_access"}


def test_detect_apply_adds_only_new(env, monkeypatch):
    found = [
        smctl.Found("services", "nginx", {"kind": "systemd", "unit": "nginx"}),
        smctl.Found("logs", "syslog", "/var/log/syslog"),
    ]
    monkeypatch.setattr(smctl, "detect", lambda: (found, []))
    assert smctl.main(["detect", "--apply"]) == 0
    assert smctl.main(["detect", "--apply"]) == 0  # second run: nothing new, no error
    data = read(env)
    assert (
        data["services"]["nginx"]["unit"] == "nginx" and data["logs"]["syslog"] == "/var/log/syslog"
    )
