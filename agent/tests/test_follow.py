"""4.3: live log follow (declared files and systemd journals)."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_monitor_agent as sma  # noqa: E402
from test_agent import FakeRelay, write_config  # noqa: E402


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(sma, "FLUSH_EVERY", 0.2)
    log = tmp_path / "app.log"
    log.write_text("old 1\nold 2\n")
    cfg = sma.load_config(write_config(tmp_path, f"""
        [services.web]
        kind = "systemd"
        unit = "nginx.service"
        [services.api]
        kind = "docker"
        [logs]
        app = "{log}"
    """))  # fmt: skip
    return cfg, log


def output(relay):
    return "".join(m["output"] for m in relay.sent)


def test_follow_streams_new_lines_through_rotation_with_a_filter(setup):
    cfg, log = setup
    relay = FakeRelay()
    ex = sma.Executor(cfg, relay)
    args = {"name": "app", "filter": "ERROR", "seconds": 10}

    def writer():
        time.sleep(0.4)
        with log.open("a") as fh:
            fh.write("INFO fine\nERROR first\n")
        time.sleep(0.4)
        log.rename(log.with_suffix(".1"))  # logrotate
        log.write_text("ERROR after rotation\nINFO skip\n")
        time.sleep(1.0)
        ex.cancel("f1")

    threading.Thread(target=writer).start()
    ex._run("f1", "tail_follow", args)
    text = output(relay)
    assert "ERROR first" in text and "ERROR after rotation" in text and "rotated" in text
    assert "INFO" not in text and "old 1" not in text  # filtered
    final = relay.sent[-1]
    assert final["status"] == "succeeded" and "stopped from the dashboard" in final["output"]
    assert any(m["status"] == "running" for m in relay.sent)  # streamed while following


def test_follow_ends_by_itself(setup, monkeypatch):
    cfg, _ = setup
    relay = FakeRelay()
    plan = sma.plan_command(cfg, "tail_follow", {"name": "app", "seconds": 1})
    plan.timeout = 0.5
    monkeypatch.setattr(sma, "plan_command", lambda *a: plan)
    sma.Executor(cfg, relay)._run("f2", "tail_follow", {})
    assert relay.sent[-1]["status"] == "succeeded" and "old 2" in output(relay)
    assert "stopped following" in relay.sent[-1]["output"]


def test_follow_only_declared_logs_and_systemd_journals(setup):
    cfg, _ = setup
    plan = sma.plan_command(
        cfg, "tail_follow", {"source": "journal", "name": "web", "seconds": 9999}
    )
    assert plan.argv[:3] == ["journalctl", "--unit=nginx.service", "--follow"]
    assert plan.follow and plan.timeout == sma.FOLLOW_MAX_SECONDS
    for args in ({"name": "secret"}, {"source": "journal", "name": "api"},
                 {"source": "journal", "name": "nope"}):  # fmt: skip
        with pytest.raises(sma.Rejected):
            sma.plan_command(cfg, "tail_follow", args)


def test_journal_follow_filters_and_ends_cleanly(setup, monkeypatch):
    cfg, _ = setup
    relay = FakeRelay()
    plan = sma.Plan(argv=["sh", "-c", "echo keep me; echo drop; sleep 30"], timeout=1,
                    follow=True, line_filter="keep")  # fmt: skip
    monkeypatch.setattr(sma, "plan_command", lambda *a: plan)
    sma.Executor(cfg, relay)._run("f3", "tail_follow", {})
    assert relay.sent[-1]["status"] == "succeeded" and relay.sent[-1]["exit_code"] == 0
    assert "keep me" in output(relay) and "drop" not in output(relay)


def test_follow_is_advertised(setup):
    assert "log_follow" in sma.Collector(setup[0]).capabilities()["features"]
