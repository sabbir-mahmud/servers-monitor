"""3.3: agent upgrades requested from the dashboard (agent asks, root re-checks and installs)."""

import json
import os
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
import server_monitor_agent as sma  # noqa: E402
import smctl  # noqa: E402
import test_smctl  # noqa: E402
from test_agent import FakeRelay, write_config  # noqa: E402

CID = "0192f0c4-7a1e-7c3e-9c1a-2b3c4d5e6f70"
SUMS = dict.fromkeys(sma.UPGRADE_FILES, "a" * 64)
env = test_smctl.env  # the smctl-in-a-temp-dir fixture


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("STATE_DIRECTORY", str(d))
    monkeypatch.setattr(smctl, "UPGRADE_REQUEST", d / "upgrade-request.json")
    return d


@pytest.fixture
def opted_in(tmp_path):
    return sma.load_config(write_config(tmp_path, "[agent]\nremote_upgrade = true\n"))


def test_agent_only_asks_when_opted_in(tmp_path, state):
    off = sma.load_config(write_config(tmp_path))
    assert "remote_upgrade" not in sma.Collector(off).capabilities()["features"]
    with pytest.raises(sma.Rejected, match="remote upgrades are off"):
        sma.plan_command(off, "_upgrade", {"ref": "v2.3.0", "sums": SUMS}, CID)
    assert not list(state.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        {"ref": "main", "sums": SUMS},  # branches move: release tags only
        {"ref": "v2.3.0; rm -rf /", "sums": SUMS},
        {"ref": "v2.3.0", "sums": {**SUMS, "agent": "zz"}},
        {"ref": "v2.3.0", "sums": {k: v for k, v in SUMS.items() if k != "smctl"}},
    ],
)
def test_agent_refuses_bad_requests(opted_in, state, args):
    with pytest.raises(sma.Rejected):
        sma.plan_command(opted_in, "_upgrade", args, CID)
    assert not list(state.iterdir())


def test_agent_writes_the_request_and_waits_for_root(opted_in, state):
    relay = FakeRelay()
    sma.Executor(opted_in, relay)._run(CID, "_upgrade", {"ref": "v2.3.0", "sums": SUMS})
    assert [m["status"] for m in relay.sent] == ["running"]  # root reports the result
    req = json.loads((state / "upgrade-request.json").read_text())
    assert req["id"] == CID and req["ref"] == "v2.3.0" and req["sums"] == SUMS
    assert "remote_upgrade" in sma.Collector(opted_in).capabilities()["features"]


def write_request(state, **override):
    body = {"id": CID, "ref": "v2.3.0", "sums": SUMS, **override}
    (state / "upgrade-request.json").write_text(json.dumps(body))


def test_root_reads_only_a_small_regular_file_and_removes_it(state, tmp_path):
    secret = tmp_path / "shadow"
    secret.write_text("root:$6$secret")
    os.symlink(secret, state / "upgrade-request.json")  # agent user points root elsewhere
    with pytest.raises(smctl.Fail, match="not a regular file") as exc:
        smctl.read_upgrade_request()
    assert "secret" not in str(exc.value) and not (state / "upgrade-request.json").exists()
    (state / "upgrade-request.json").write_text("x" * 5000)
    with pytest.raises(smctl.Fail, match="small regular file"):
        smctl.read_upgrade_request()
    write_request(state)
    assert smctl.read_upgrade_request() == {"id": CID, "ref": "v2.3.0", "sums": SUMS}
    assert not (state / "upgrade-request.json").exists()


@pytest.mark.parametrize(
    "override", [{"ref": "main"}, {"id": "../x"}, {"sums": {"agent": "a" * 64}}]
)
def test_root_rechecks_every_field(state, override):
    write_request(state, **override)
    with pytest.raises(smctl.Fail):
        smctl.read_upgrade_request()


def test_root_refuses_unless_opted_in_in_its_own_config(env, state, monkeypatch):
    reports, installs = [], []
    monkeypatch.setattr(smctl, "report_result", lambda *a: reports.append(a) or True)
    monkeypatch.setattr(smctl, "_upgrade", lambda *a, **k: installs.append(a) or (0, ""))
    write_request(state)
    with pytest.raises(smctl.Fail, match="remote upgrades are off"):
        smctl.upgrade_from_request()
    assert installs == [] and reports[0][:2] == (CID, 1)


def test_root_installs_the_tag_from_the_recorded_repo_and_reports(env, state, monkeypatch):
    data = smctl.load()
    data["agent"]["remote_upgrade"] = True
    env.write_text(smctl.dump_toml(data))
    reports, installs = [], []
    monkeypatch.setattr(smctl, "report_result", lambda *a: reports.append(a) or True)
    monkeypatch.setattr(smctl, "_upgrade",
                        lambda ref, sums, capture=False: installs.append((ref, sums)) or (0, "\x1b[32m✓\x1b[0m done\n"))  # fmt: skip
    write_request(state)
    assert smctl.upgrade_from_request() == 0
    ref, sums = installs[0]
    assert ref == "v2.3.0" and sums.split(",")[0] == f"install={'a' * 64}" and "uservice=" in sums
    assert reports == [(CID, 0, "✓ done\n")]  # colour codes stripped


def test_command_result_is_signed_and_sent_by_the_agent(tmp_path, monkeypatch):
    cfg = sma.load_config(write_config(tmp_path))
    sent = []

    class Relay:
        def __init__(self, cfg):
            pass

        def send(self, message):
            sent.append(message)

        def close(self):
            pass

    monkeypatch.setattr(sma, "Relay", Relay)
    monkeypatch.setattr(
        sys,
        "stdin",
        type("S", (), {"read": lambda self: "installed\n", "isatty": lambda self: False})(),
    )
    assert sma.send_command_result(cfg, CID, "succeeded", 0) == 0
    assert sent == [
        {"t": "res", "id": CID, "status": "succeeded", "output": "installed\n", "exit_code": 0}
    ]
    assert sma.send_command_result(cfg, "not-an-id", "failed", 1) == 2


def test_units_watch_the_request_and_run_smctl():
    path = (AGENT_DIR / "server-monitor-upgrade.path").read_text()
    service = (AGENT_DIR / "server-monitor-upgrade.service").read_text()
    assert "PathExists=/var/lib/server-monitor-agent/upgrade-request.json" in path
    assert "smctl upgrade --from-request" in service and "Type=oneshot" in service
