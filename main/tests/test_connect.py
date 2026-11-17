"""Connecting a computer with one link and one pasted line (apps/devices/connect.py)."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from django.urls import reverse

from apps.devices.models import Device

from . import test_devices
from .test_devices import add_device, api_pair

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
    resp = api_pair(client, code, platform="macos", preview=True)
    assert resp.status_code == 200 and "token" not in resp.json()
    disclosure = resp.json()["disclosure"]
    assert disclosure.startswith("The Rahmans will see") and "system version" in disclosure
    assert "location" not in disclosure  # computers don't send it, so don't claim it
    device.refresh_from_db()
    assert device.pairing_code_hash and not device.is_paired  # nothing used up
    assert api_pair(client, code, platform="macos").status_code == 201


def test_text_answers_for_the_shell_installer(client, parent, sara):
    client.force_login(parent)
    _, code = add_device(client, sara, platform="linux", name="Sara's laptop")
    body = json.dumps({"code": code, "platform": "linux"})
    resp = client.post(reverse("device_api_pair"), body, content_type="application/json",
                       HTTP_ACCEPT="text/plain")  # fmt: skip
    text = resp.content.decode()
    assert resp.status_code == 201 and resp["Content-Type"].startswith("text/plain")
    assert "\ntoken=ngd_" in "\n" + text and "device_name=Sara's laptop" in text
    assert "device_account=The Rahmans" in text and "checkin_seconds=" in text
    bad = client.post(reverse("device_api_pair"), body, content_type="application/json",
                      HTTP_ACCEPT="text/plain")  # fmt: skip
    assert bad.status_code == 404 and bad.content.decode().startswith("error=")


@pytest.mark.skipif(THIS_OS is None or not shutil.which("curl"), reason="needs sh and curl")
@pytest.mark.django_db(transaction=True)
def test_the_installer_pairs_and_checks_in_for_real(live_server, settings, tmp_path):
    """Run the real install.sh with sh and curl against a live dashboard, in a throwaway
    HOME, without scheduling anything on this machine."""
    from apps.devices import services
    from apps.tenants.models import Account

    settings.DASHBOARD_URL = live_server.url
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'connected as "Test laptop" in The Rahmans' in result.stdout

    device.refresh_from_db()
    assert device.is_paired and device.last_seen_at and device.os_version
    assert device.storage_total and device.app_version.startswith("nexguard-device-sh")
    installed = home / ("Library/Application Support/NexGuard" if THIS_OS == "macos"
                        else ".local/share/nexguard")  # fmt: skip
    config = (installed / "config").read_text()
    assert "TOKEN='ngd_" in config and oct((installed / "config").stat().st_mode)[-3:] == "600"

    # Removed on the dashboard: the next check-in uninstalls itself.
    device.delete()
    subprocess.run(["sh", str(installed / "checkin.sh"), "now"], env=env, timeout=60)
    assert not installed.exists()
