import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_monitor_agent as sma  # noqa: E402


def write_config(tmp_path, body=""):
    path = tmp_path / "agent.toml"
    path.write_text(
        textwrap.dedent("""
        [main]
        redis_url = "rediss://sm-srv1:tok@relay.test:6379"
    """)
        + textwrap.dedent(body)
    )
    return path


@pytest.fixture
def cfg(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("".join(f"line {i}\n" for i in range(5000)))
    return sma.load_config(
        write_config(
            tmp_path,
            f'''
        [services.nginx]
        kind = "systemd"
        restartable = true
        [services.cache]
        kind = "systemd"
        [logs]
        app = "{log}"
        [actions.deploy]
        command = ["echo", "deployed"]
        timeout = 5
    ''',
        )
    )


def test_requires_tls_unless_opted_out(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('[main]\nredis_url = "redis://sm-abc:pw@localhost:6379"\n')
    with pytest.raises(sma.ConfigError, match="rediss"):
        sma.load_config(p)
    p.write_text('[main]\nredis_url = "redis://sm-abc:pw@localhost:6379"\nallow_insecure = true\n')
    cfg = sma.load_config(p)
    assert cfg.server_id == "abc" and cfg.report_interval == 120


def test_server_id_comes_from_the_relay_username(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('[main]\nredis_url = "rediss://default:pw@relay.test:6379"\n')
    with pytest.raises(sma.ConfigError, match="server's id"):
        sma.load_config(p)


def test_builtin_names_cannot_be_shadowed(tmp_path):
    with pytest.raises(sma.ConfigError, match="reserved"):
        sma.load_config(write_config(tmp_path, '[actions.shell]\ncommand = ["sh"]\n'))


def test_unknown_action_rejected(cfg):
    with pytest.raises(sma.Rejected):
        sma.plan_command(cfg, "rm_everything", {})


def test_shell_disabled_by_default(cfg):
    with pytest.raises(sma.Rejected, match="disabled"):
        sma.plan_command(cfg, "shell", {"command": "id"})


def test_tail_only_declared_logs(cfg):
    with pytest.raises(sma.Rejected):
        sma.plan_command(cfg, "tail_log", {"name": "/etc/shadow"})
    with pytest.raises(sma.Rejected):
        sma.plan_command(cfg, "tail_log", {"name": "../app"})


def test_tail_returns_last_lines_and_caps(cfg):
    out = sma.plan_command(cfg, "tail_log", {"name": "app", "lines": 3}).inline_output
    assert out == "line 4997\nline 4998\nline 4999\n"
    out = sma.plan_command(cfg, "tail_log", {"name": "app", "lines": 10**6}).inline_output
    assert len(out.splitlines()) == sma.MAX_TAIL_LINES


def test_restart_only_restartable(cfg, monkeypatch):
    monkeypatch.setattr(sma.os, "geteuid", lambda: 1000)
    plan = sma.plan_command(cfg, "restart_service", {"name": "nginx"})
    assert plan.argv == ["sudo", "-n", "systemctl", "restart", "nginx"]
    with pytest.raises(sma.Rejected):
        sma.plan_command(cfg, "restart_service", {"name": "cache"})


def test_custom_action_args_are_ignored(cfg):
    # Arguments from main can never change a declared action's argv.
    plan = sma.plan_command(cfg, "deploy", {"command": "; rm -rf /"})
    assert plan.argv == ["echo", "deployed"]


class FakeRelay:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def test_executor_runs_and_reports(cfg):
    relay = FakeRelay()
    sma.Executor(cfg, relay)._run("c1", "deploy", {})
    final = relay.sent[-1]
    assert final["t"] == "res" and final["id"] == "c1"
    assert final["status"] == "succeeded" and final["exit_code"] == 0
    assert "deployed" in "".join(m["output"] for m in relay.sent)


def test_executor_timeout_kills_process(tmp_path):
    cfg = sma.load_config(
        write_config(tmp_path, '[actions.slow]\ncommand = ["sleep", "30"]\ntimeout = 1\n')
    )
    relay = FakeRelay()
    sma.Executor(cfg, relay)._run("c2", "slow", {})
    assert relay.sent[-1]["status"] == "failed" and "timeout" in relay.sent[-1]["output"]


def test_executor_cancel_stops_a_running_command(tmp_path):
    import threading

    cfg = sma.load_config(
        write_config(tmp_path, '[actions.slow]\ncommand = ["sleep", "30"]\ntimeout = 60\n')
    )
    relay = FakeRelay()
    ex = sma.Executor(cfg, relay)
    threading.Timer(0.5, ex.cancel, args=("c9",)).start()
    ex._run("c9", "slow", {})
    assert relay.sent[-1]["status"] == "failed" and "cancelled" in relay.sent[-1]["output"]


def test_executor_reports_rejection(cfg):
    relay = FakeRelay()
    sma.Executor(cfg, relay)._run("c3", "nope", {})
    assert relay.sent[-1]["status"] == "failed"
    assert "rejected by agent" in relay.sent[-1]["output"]


def test_agent_handles_relay_messages(cfg, monkeypatch):
    import time

    agent = sma.Agent(cfg)
    relay, submitted = FakeRelay(), []
    agent.relay = relay
    monkeypatch.setattr(agent.executor, "submit", submitted.append)
    agent.handle({"t": "cmd", "id": "old", "action": "deploy", "exp": time.time() - 1})
    assert submitted == [] and relay.sent == []  # expired while offline: never runs
    agent.handle({"t": "cmd", "id": "new", "action": "deploy", "exp": time.time() + 60})
    assert [c["id"] for c in submitted] == ["new"] and relay.sent == [{"t": "ack", "id": "new"}]
    agent.handle({"t": "cfg", "report_interval": 300})
    assert agent.report_interval == 300
    agent.handle({"t": "cancel", "id": "new"})
    assert agent.executor._is_cancelled("new")


def test_agent_code_runs_on_python_311():
    """Servers run their system Python (Debian 12 = 3.11). Guard against newer syntax."""
    import ast

    for name in ("server_monitor_agent.py", "smctl.py"):
        source = (Path(__file__).resolve().parents[1] / name).read_text()
        ast.parse(source, feature_version=(3, 11))


def test_report_shape_matches_main_schema(cfg):
    report = sma.Collector(cfg).report()
    json.dumps(report)
    assert set(report) == {"agent_version", "host", "metrics", "disks", "services", "capabilities"}
    assert report["capabilities"]["logs"] == ["app"]
    assert 0 <= report["metrics"]["memory_percent"] <= 100
