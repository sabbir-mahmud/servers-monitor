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
        url = "https://monitor.test"
        token = "smk_test"
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


def test_requires_https_unless_opted_out(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('[main]\nurl = "http://x"\ntoken = "t"\n')
    with pytest.raises(sma.ConfigError, match="https"):
        sma.load_config(p)
    p.write_text('[main]\nurl = "http://x"\ntoken = "t"\nallow_insecure_http = true\n')
    assert sma.load_config(p).url == "http://x"


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


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


def test_executor_runs_and_reports(cfg):
    api = FakeApi()
    ex = sma.Executor(cfg, api)
    ex._run("c1", "deploy", {})
    final = api.calls[-1][2]
    assert final["status"] == "succeeded" and final["exit_code"] == 0
    assert "deployed" in "".join(c[2]["output"] for c in api.calls)


def test_executor_timeout_kills_process(tmp_path):
    cfg = sma.load_config(
        write_config(tmp_path, '[actions.slow]\ncommand = ["sleep", "30"]\ntimeout = 1\n')
    )
    api = FakeApi()
    sma.Executor(cfg, api)._run("c2", "slow", {})
    final = api.calls[-1][2]
    assert final["status"] == "failed" and "timeout" in final["output"]


def test_executor_reports_rejection(cfg):
    api = FakeApi()
    sma.Executor(cfg, api)._run("c3", "nope", {})
    assert api.calls[-1][2]["status"] == "failed"
    assert "rejected by agent" in api.calls[-1][2]["output"]


def test_report_shape_matches_main_schema(cfg):
    report = sma.Collector(cfg).report()
    json.dumps(report)
    assert set(report) == {"agent_version", "host", "metrics", "disks", "services", "capabilities"}
    assert report["capabilities"]["logs"] == ["app"]
    assert 0 <= report["metrics"]["memory_percent"] <= 100
