"""The Go device agent (device-agent/) against the dashboard's relay: build it, pair, check
in, and forget the pairing when the device is removed. It never talks to the dashboard
itself, so there is no web server here: only a Redis-protocol relay (tests/resp_server.py)
whose messages go straight to the bridge. Skipped where Go isn't installed.

It runs as the current user with NEXGUARD_CONFIG_DIR pointing at a temporary folder, and
never installs a service on the machine running the tests."""

import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.agent_api import relay
from apps.devices import services
from apps.devices.models import Device, DeviceControls
from apps.tenants.models import Account, Person

from .resp_server import RespServer

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


@pytest.fixture
def pair_url(fake_relay, settings):
    """The relay address the install script would pass to `nexguard pair`."""
    server = RespServer(fake_relay)
    settings.REDIS_URL = f"redis://default:admin@127.0.0.1:{server.port}"
    with server:
        yield relay.computer_pair_url()


def run(binary, *args, config_dir, stdin="", hosts=None):
    env = {**os.environ, "NEXGUARD_CONFIG_DIR": str(config_dir)}
    if hosts is not None:
        env["NEXGUARD_HOSTS"] = str(hosts)  # never the real hosts file
    # Its own session: no controlling terminal, so it can't open /dev/tty to ask, even when
    # the tests run in one, and reads the answer from stdin instead.
    return subprocess.run([str(binary), *args], input=stdin, env=env, capture_output=True,
                          text=True, timeout=60, start_new_session=True)  # fmt: skip


def new_device(person_kind=None):
    platform = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    account = Account.objects.create(name="The Rahmans", kind="family")
    person = (
        Person.objects.create(account=account, kind=person_kind, name="Sara")
        if person_kind
        else None
    )
    device = Device.objects.create(account=account, platform=platform, name="Sara's laptop",
                                   person=person)  # fmt: skip
    return device, services.new_pairing_code(device)


def test_agent_pairs_checks_in_and_reports_health(pair_url, agent_binary, tmp_path):
    device, code = new_device()
    result = run(agent_binary, "pair", "--yes", pair_url, code, config_dir=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will see this device's last online time, CPU, memory, GPU" in result.stdout
    assert 'Paired as "Sara\'s laptop" in The Rahmans' in result.stdout

    device.refresh_from_db()
    assert device.is_paired and device.last_seen_at
    assert device.app_version == "nexguard-agent 9.9.9" and device.os_version
    assert device.cpu_percent is not None and device.memory_total and device.storage_total
    assert device.samples.count() == 1

    config = json.loads((tmp_path / "config.json").read_text())
    assert config["relay"]["url"].startswith(f"redis://sm-d-{device.pk}:")
    assert config["relay"]["inbox"] == relay.device_inbox_key(device.pk)
    assert len(config["key"]) == 64 and "server" not in config and "token" not in config
    assert config["settings_digest"]  # it read the settings the dashboard left
    if os.name != "nt":
        assert oct((tmp_path / "config.json").stat().st_mode)[-3:] == "600"

    status = run(agent_binary, "status", config_dir=tmp_path).stdout
    assert 'Paired as "Sara\'s laptop"' in status and "OK" in status
    assert "through redis://127.0.0.1:" in status and "admin" not in status

    # The relay keeps one check-in per 30 s; this one comes "later".
    Device.objects.filter(pk=device.pk).update(last_seen_at=timezone.now() - timedelta(minutes=5))
    assert run(agent_binary, "checkin", config_dir=tmp_path).returncode == 0
    assert device.samples.count() == 2


def test_saying_no_keeps_the_code_and_saves_nothing(pair_url, agent_binary, tmp_path):
    device, code = new_device()
    result = run(agent_binary, "pair", pair_url, code, config_dir=tmp_path, stdin="n\n")
    assert result.returncode == 1 and "you said no" in result.stderr
    assert not (tmp_path / "config.json").exists()
    device.refresh_from_db()
    assert not device.is_paired and device.pairing_code_hash  # code still usable


def test_removed_on_the_dashboard_the_agent_forgets_the_pairing(pair_url, agent_binary, tmp_path):
    device, code = new_device()
    assert run(agent_binary, "pair", "--yes", pair_url, code, config_dir=tmp_path).returncode == 0
    device.delete()
    result = run(agent_binary, "checkin", config_dir=tmp_path)
    assert result.returncode == 1 and "removed this device" in result.stderr
    assert not (tmp_path / "config.json").exists()
    assert "Not paired" in run(agent_binary, "status", config_dir=tmp_path).stdout


def test_a_wrong_code_shows_the_dashboards_words(pair_url, agent_binary, tmp_path):
    new_device()  # a code is out, so the relay process listens for pairing
    result = run(agent_binary, "pair", "--yes", pair_url, "K7PQ-3MXA", config_dir=tmp_path)
    assert result.returncode == 1 and "isn't valid" in result.stderr


def test_an_old_http_pairing_is_told_to_pair_again(agent_binary, tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"server": "https://monitor.example.com", "token": "ngd_x", "interval": 300})
    )
    result = run(agent_binary, "checkin", config_dir=tmp_path)
    assert result.returncode == 1 and "pair it again" in result.stderr


def test_a_childs_computer_gets_blocking_in_its_hosts_file(
    pair_url, agent_binary, tmp_path, settings
):
    settings.ADULT_BLOCKLIST_URL = ""
    device, code = new_device(person_kind="child")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    config = tmp_path / "config"
    result = run(agent_binary, "pair", "--yes", pair_url, code, config_dir=config, hosts=hosts)
    assert result.returncode == 0, result.stderr
    assert "screen time" in result.stdout  # the consent names it for a child's computer
    DeviceControls.objects.create(device=device, screen_time=True, blocked_domains="tiktok.com")
    services.publish_settings(device)  # as saving them on the Controls page does

    assert run(agent_binary, "checkin", config_dir=config, hosts=hosts).returncode == 0
    text = hosts.read_text()
    assert text.startswith("127.0.0.1 localhost\n") and "0.0.0.0 tiktok.com" in text
    assert "0.0.0.0 www.tiktok.com" in text
    status = run(agent_binary, "status", config_dir=config, hosts=hosts).stdout
    assert "Screen time: on" in status and "Blocking: " in status and "sites" in status

    device.delete()  # removed on the dashboard: blocking goes with the pairing
    run(agent_binary, "checkin", config_dir=config, hosts=hosts)
    assert hosts.read_text() == "127.0.0.1 localhost\n"
