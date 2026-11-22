"""Connecting a computer with one link and one pasted line (apps/devices/connect.py)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from apps.devices.models import Device

from . import test_devices
from .computers import ask, pair_computer
from .resp_server import RespServer
from .test_devices import add_device

# The family, parent and child fixtures from the device tests.
family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

pytestmark = pytest.mark.django_db

THIS_OS = {"darwin": "macos", "linux": "linux"}.get(sys.platform)


def test_pair_page_for_a_computer_gives_a_link_to_open_there(client, parent, sara):
    client.force_login(parent)
    device, code, page = add_device(client, sara, platform="windows", name="Family PC",
                                    with_page=True)  # fmt: skip
    assert f"/d/{code}</code>" in page and "Copy link" in page
    assert f"irm http://testserver/d/{code.replace('-', '')}/install.ps1 | iex" in page
    assert "curl -fsSL" not in page  # only the Windows line for a Windows PC
    assert "Scan the QR code" not in page and "python3" not in page


def test_link_page_needs_no_sign_in_and_shows_every_system(client):
    resp = client.get("/d/K7PQ-3MXA/")
    page = resp.content.decode()
    assert resp.status_code == 200 and "Connect this computer" in page
    assert "irm http://testserver/d/K7PQ3MXA/install.ps1 | iex" in page
    assert "curl -fsSL http://testserver/d/K7PQ3MXA/install.sh | sh" in page
    assert "Open PowerShell" in page and "Open Terminal" in page
    # The code is in the URL: not cached, not indexed, not sent on as a Referer.
    assert resp["Cache-Control"] == "no-store" and resp["Referrer-Policy"] == "no-referrer"
    assert "noindex" in resp["X-Robots-Tag"]


def test_link_page_and_scripts_never_look_the_code_up(client, django_assert_num_queries):
    # Well-formed is enough to render: no database hit, so no way to test codes here.
    client.get("/d/AAAABBBB/install.sh")  # the first one ever fetches the relay's login
    with django_assert_num_queries(0):
        assert client.get("/d/K7PQ3MXA/install.sh").status_code == 200
    for bad in ("/d/K7PQ/", "/d/K7PQ3MX0/", "/d/<script>/", "/d/K7PQ3MXAB/install.sh"):
        assert client.get(bad).status_code == 404, bad
    # Punctuation is stripped like dashes are, so only the clean code reaches the script.
    sh = client.get("/d/K7PQ3MXA%27;%23/install.sh").content.decode()
    assert "CODE='K7PQ3MXA'" in sh and "MXA'" + ";" not in sh
    assert client.get("/d/K7PQ3MXA%27;id/install.sh").status_code == 404  # 10 letters


def test_scripts_carry_the_server_and_code_and_nothing_else(client, settings):
    settings.DASHBOARD_URL = "https://monitor.example.com/"
    sh = client.get("/d/k7pq-3mxa/install.sh").content.decode()
    assert "SERVER='https://monitor.example.com'" in sh and "CODE='K7PQ3MXA'" in sh
    assert sh.startswith("#!/bin/sh") and "\r" not in sh and "{%" not in sh
    ps1 = client.get("/d/K7PQ3MXA/install.ps1").content
    assert b"$NgServer = 'https://monitor.example.com'" in ps1 and b"$NgCode = 'K7PQ3MXA'" in ps1
    # Windows PowerShell 5.1 reads scripts in the system code page: keep it pure ASCII.
    assert ps1.isascii() and b"\r\n" in ps1 and b"{%" not in ps1
    for path in ("/d/uninstall.sh", "/d/uninstall.ps1"):
        assert client.get(path).status_code == 200


def test_a_dashboard_url_that_could_break_out_of_quotes_is_refused(client, settings):
    settings.DASHBOARD_URL = "https://evil.example.com/';rm -rf ~;'"
    assert client.get("/d/K7PQ3MXA/install.sh").status_code == 404


@pytest.mark.parametrize("path", ["/d/K7PQ3MXA/install.sh", "/d/uninstall.sh"])
def test_shell_scripts_are_valid_posix_sh(client, path, tmp_path):
    script = tmp_path / "s.sh"
    script.write_bytes(client.get(path).content)
    for shell in ("sh", "dash", "bash"):
        if shutil.which(shell):
            result = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
            assert result.returncode == 0, (shell, result.stderr)
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-s", "sh", "-S", "warning", str(script)],
                                capture_output=True, text=True)  # fmt: skip
        assert result.returncode == 0, result.stdout


def test_preview_shows_who_will_see_it_and_keeps_the_code(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    preview = ask(code, {"platform": "macos"}, preview=True)
    assert preview["state"] == "preview" and "relay" not in preview
    disclosure = preview["disclosure"]
    assert disclosure.startswith("The Rahmans will see") and "system version" in disclosure
    assert "location" not in disclosure  # computers don't send it, so don't claim it
    device.refresh_from_db()
    assert device.pairing_code_hash and not device.is_paired  # nothing used up
    pair_computer(device, code)


def test_scripts_carry_the_relays_pairing_login_only(client, fake_relay):
    sh = client.get("/d/K7PQ3MXA/install.sh").content.decode()
    relay_url = sh.split("RELAY='")[1].split("'")[0]
    assert relay_url.startswith("rediss://sm-pc:") and relay_url.endswith(
        "@relay.test:6379/?prefix=sm"
    )
    rules = fake_relay.users["sm-pc"]
    assert "+rpush" in rules and "+get" in rules and "+blpop" not in rules
    assert "~sm:pair:in" in rules and "~sm:pair:out:*" in rules and len(
        [r for r in rules if r.startswith("~")]) == 2  # fmt: skip
    # The same login for every script (and it survives a restart), not one per download.
    assert f"RELAY='{relay_url}'" in client.get("/d/AAAABBBB/install.sh").content.decode()
    ps1 = client.get("/d/K7PQ3MXA/install.ps1").content.decode()
    assert f"$NgRelay = '{relay_url}'" in ps1


def test_without_a_relay_the_scripts_say_so(client, fake_relay):
    fake_relay.down = True
    sh = client.get("/d/K7PQ3MXA/install.sh").content.decode()
    assert "RELAY=''" in sh and "can't take computers right now" in sh


@pytest.mark.skipif(THIS_OS is None or not shutil.which("curl"), reason="needs sh and curl")
@pytest.mark.django_db(transaction=True)
def test_without_an_agent_build_the_installer_stops(live_server, settings, tmp_path):
    """The health-only check-in script is gone (it spoke HTTP to the dashboard): with no
    agent build to install, nothing is paired and the person is told why."""
    from apps.devices import services
    from apps.tenants.models import Account

    settings.DASHBOARD_URL = live_server.url
    settings.DEVICE_AGENT_DIR = tmp_path / "no-builds"
    account = Account.objects.create(name="The Rahmans", kind="family")
    device = Device.objects.create(account=account, platform=THIS_OS, name="Test laptop")
    code = services.new_pairing_code(device).replace("-", "")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "NEXGUARD_YES": "1",
           "NEXGUARD_NO_SCHEDULE": "1", "XDG_DATA_HOME": str(home / ".local/share")}  # fmt: skip
    script = f"curl -fsSL {live_server.url}/d/{code}/install.sh | sh"
    result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True,
                            timeout=60)  # fmt: skip
    assert result.returncode == 1 and "no NexGuard agent for this computer" in result.stderr
    device.refresh_from_db()
    assert not device.is_paired and device.pairing_code_hash


@pytest.mark.parametrize("path", ["/d/K7PQ3MXA/install.sh", "/d/uninstall.sh"])
def test_uninstall_only_stops_what_this_install_scheduled(client, path):
    """launchd jobs and the crontab belong to the login, not to $HOME: an install made under
    another HOME (as the tests above do) must never stop the computer's real check-in."""
    script = client.get(path).content.decode()
    assert 'if [ -f "$PLIST" ]; then\n      launchctl bootout' in script
    assert 'grep -vF "$DIR/" | crontab -' in script
    assert "grep -v nexguard | crontab -" not in script.split("run every 5 minutes")[0]


GO = shutil.which("go") or (
    "/opt/homebrew/bin/go" if os.path.exists("/opt/homebrew/bin/go") else None
)


@pytest.fixture(scope="module")
def agent_builds(tmp_path_factory):
    """This computer's agent, built from source the way build.sh names it, with checksums."""
    import hashlib
    import platform

    out = tmp_path_factory.mktemp("builds")
    goos = {"macos": "darwin", "linux": "linux"}[THIS_OS]
    arch = {"arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(), "amd64")
    name = f"nexguard-{goos}-{arch}"
    src = os.path.join(os.path.dirname(__file__), "..", "..", "device-agent")
    subprocess.run([GO, "build", "-ldflags", "-X main.Version=9.9.9", "-o", str(out / name), "."],
                   cwd=src, check=True, env={**os.environ, "CGO_ENABLED": "0"},
                   timeout=300)  # fmt: skip
    digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {name}\n")
    return out


@pytest.mark.skipif(THIS_OS is None or not shutil.which("curl") or GO is None,
                    reason="needs sh, curl and Go")  # fmt: skip
@pytest.mark.django_db(transaction=True)
def test_the_installer_installs_the_agent_as_the_user(
    live_server, settings, tmp_path, agent_builds, fake_relay
):
    """With agent builds on the dashboard, the one-line command installs the agent running
    as the person (no sudo): screen time and websites can be turned on, blocking can't. The
    agent pairs and checks in through a real Redis-protocol relay, never to the dashboard."""
    from apps.devices import services
    from apps.tenants.models import Account

    relay_server = RespServer(fake_relay)
    settings.REDIS_URL = f"redis://default:admin@127.0.0.1:{relay_server.port}"
    settings.DASHBOARD_URL = live_server.url
    settings.DEVICE_AGENT_DIR = agent_builds
    settings.ADULT_BLOCKLIST_URL = ""
    account = Account.objects.create(name="Solo Dev", kind="solo")
    device = Device.objects.create(account=account, platform=THIS_OS, name="My laptop")
    code = services.new_pairing_code(device).replace("-", "")
    home = tmp_path / "home"
    home.mkdir()
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "NEXGUARD_YES": "1",
           "NEXGUARD_NO_SCHEDULE": "1", "XDG_DATA_HOME": str(home / ".local/share"),
           "NEXGUARD_HOSTS": str(hosts), "NEXGUARD_USER_INSTALL": "1"}  # fmt: skip
    script = f"curl -fsSL {live_server.url}/d/{code}/install.sh | sh"
    with relay_server:
        result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True,
                                timeout=120)  # fmt: skip
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'Paired as "My laptop" in Solo Dev' in result.stdout
        _check_the_installed_agent(result, device, home, hosts, env)


def _check_the_installed_agent(result, device, home, hosts, env):
    from apps.devices.models import DeviceControls

    # The agent asked with the full disclosure: screen time and websites, never blocking.
    assert "screen time" in result.stdout and "blocking" not in result.stdout

    device.refresh_from_db()
    assert device.app_version == "nexguard-agent 9.9.9" and device.agent_mode == "user"
    assert device.controls_allowed and not device.can_block
    assert device.cpu_percent is not None and device.samples.count() == 1  # its first check-in
    installed = home / ("Library/Application Support/NexGuard" if THIS_OS == "macos"
                        else ".local/share/nexguard")  # fmt: skip
    assert (installed / "nexguard").exists() and (installed / "uninstall.sh").exists()
    config = json.loads((installed / "config.json").read_text())
    assert oct((installed / "config.json").stat().st_mode)[-3:] == "600"
    assert config["relay"]["url"].startswith(f"redis://sm-d-{device.pk}:") and config["key"]
    assert not (installed / "checkin.sh").exists()

    # Blocking chosen on the dashboard never reaches a user-mode agent.
    DeviceControls.objects.create(device=device, screen_time=True, block_adult=True,
                                  blocked_domains="tiktok.com")  # fmt: skip
    agent = [str(installed / "nexguard"), "--user", "--dir", str(installed)]
    checkin = subprocess.run([*agent, "checkin"], env=env, capture_output=True, text=True,
                             timeout=60)  # fmt: skip
    assert checkin.returncode == 0, checkin.stderr
    assert hosts.read_text() == "127.0.0.1 localhost\n"
    status = subprocess.run([*agent, "status"], env=env, capture_output=True, text=True,
                            timeout=60).stdout  # fmt: skip
    assert "Screen time: on" in status and "needs the administrator install" in status
    assert "127.0.0.1" in status and config["relay"]["url"].split("@")[0] not in status

    # Removed on the dashboard: its relay login is deleted and the agent forgets the pairing.
    device.delete()
    gone = subprocess.run([*agent, "checkin"], env=env, capture_output=True, text=True,
                          timeout=60)  # fmt: skip
    assert gone.returncode != 0 and not (installed / "config.json").exists()

    subprocess.run(["sh", str(installed / "uninstall.sh"), "quiet"], env=env, timeout=60)
    assert not installed.exists()


def test_agent_builds_are_served_by_name_only(client, settings, tmp_path):
    settings.DEVICE_AGENT_DIR = tmp_path
    (tmp_path / "nexguard-darwin-arm64").write_bytes(b"\x7fELF")
    (tmp_path / "SHA256SUMS").write_text("abc  nexguard-darwin-arm64\n")
    (tmp_path / "secret.txt").write_text("no")
    resp = client.get("/d/agent/nexguard-darwin-arm64")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"\x7fELF"
    assert resp["Content-Type"] == "application/octet-stream"
    assert client.get("/d/agent/SHA256SUMS").status_code == 200
    assert client.get("/d/agent/secret.txt").status_code == 404
    assert client.get("/d/agent/..%2Fsecret.txt").status_code == 404
    assert client.get("/d/agent/nexguard-linux-amd64").status_code == 404  # not built
