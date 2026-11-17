"""The Go device agent (device-agent/) against a live dashboard: build it, pair, check in,
and forget the pairing when the device is removed. Skipped where Go isn't installed.

It runs as the current user with NEXGUARD_CONFIG_DIR pointing at a temporary folder, and
never installs a service on the machine running the tests."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apps.devices import services
from apps.devices.models import Device
from apps.tenants.models import Account

AGENT_SRC = Path(__file__).resolve().parents[2] / "device-agent"
GO = shutil.which("go") or (
    "/opt/homebrew/bin/go" if Path("/opt/homebrew/bin/go").exists() else None
)

pytestmark = [
    pytest.mark.skipif(GO is None, reason="needs Go to build the agent"),
    pytest.mark.django_db(transaction=True),
]


@pytest.fixture(scope="module")
def agent_binary(tmp_path_factory):
    out = tmp_path_factory.mktemp("agent") / "nexguard"
    subprocess.run([GO, "build", "-ldflags", "-X main.Version=9.9.9", "-o", str(out), "."],
                   cwd=AGENT_SRC, check=True, env={**os.environ, "CGO_ENABLED": "0"},
                   timeout=300)  # fmt: skip
    return out


def run(binary, *args, config_dir, stdin=""):
    env = {**os.environ, "NEXGUARD_CONFIG_DIR": str(config_dir)}
    # Its own session: no controlling terminal, so it can't open /dev/tty to ask, even when
    # the tests run in one, and reads the answer from stdin instead.
    return subprocess.run([str(binary), *args], input=stdin, env=env, capture_output=True,
                          text=True, timeout=60, start_new_session=True)  # fmt: skip


def new_device():
    platform = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    account = Account.objects.create(name="The Rahmans", kind="family")
    device = Device.objects.create(account=account, platform=platform, name="Sara's laptop")
    return device, services.new_pairing_code(device)


def test_agent_pairs_checks_in_and_reports_health(live_server, agent_binary, tmp_path):
    device, code = new_device()
    result = run(agent_binary, "pair", "--yes", live_server.url, code, config_dir=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will see this device's last online time, CPU, memory, GPU" in result.stdout
    assert 'Paired as "Sara\'s laptop" in The Rahmans' in result.stdout

    device.refresh_from_db()
    assert device.is_paired and device.last_seen_at
    assert device.app_version == "nexguard-agent 9.9.9" and device.os_version
    assert device.cpu_percent is not None and device.memory_total and device.storage_total
    assert device.samples.count() == 1

    config = json.loads((tmp_path / "config.json").read_text())
    assert config["token"].startswith("ngd_") and config["server"] == live_server.url
    if os.name != "nt":
        assert oct((tmp_path / "config.json").stat().st_mode)[-3:] == "600"

    status = run(agent_binary, "status", config_dir=tmp_path).stdout
    assert 'Paired as "Sara\'s laptop"' in status and "OK" in status

    assert run(agent_binary, "checkin", config_dir=tmp_path).returncode == 0
    assert device.samples.count() == 2


def test_saying_no_keeps_the_code_and_saves_nothing(live_server, agent_binary, tmp_path):
    device, code = new_device()
    result = run(agent_binary, "pair", live_server.url, code, config_dir=tmp_path, stdin="n\n")
    assert result.returncode == 1 and "you said no" in result.stderr
    assert not (tmp_path / "config.json").exists()
    device.refresh_from_db()
    assert not device.is_paired and device.pairing_code_hash  # code still usable


def test_removed_on_the_dashboard_the_agent_forgets_the_pairing(
    live_server, agent_binary, tmp_path
):
    device, code = new_device()
    assert (
        run(agent_binary, "pair", "--yes", live_server.url, code, config_dir=tmp_path).returncode
        == 0
    )
    device.delete()
    result = run(agent_binary, "checkin", config_dir=tmp_path)
    assert result.returncode == 1 and "removed this device" in result.stderr
    assert not (tmp_path / "config.json").exists()
    assert "Not paired" in run(agent_binary, "status", config_dir=tmp_path).stdout


def test_a_wrong_code_shows_the_dashboards_words(live_server, agent_binary, tmp_path):
    result = run(agent_binary, "pair", "--yes", live_server.url, "K7PQ-3MXA", config_dir=tmp_path)
    assert result.returncode == 1 and "isn't valid" in result.stderr
