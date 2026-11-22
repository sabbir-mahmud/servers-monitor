"""The dashboard rebuilds the agent it serves when device-agent/ is newer than the builds."""

import os
import stat
import time

import pytest

from apps.devices import agentbuild

BUILD_SH = """#!/bin/sh
set -eu
[ -f fail ] && { echo "compile error" >&2; exit 1; }
mkdir -p "$DIST"
cat VERSION > "$DIST/nexguard-darwin-arm64"
echo "sum  nexguard-darwin-arm64" > "$DIST/SHA256SUMS"
echo built >> builds
"""


@pytest.fixture
def source(tmp_path, settings, monkeypatch):
    src = tmp_path / "device-agent"
    (src / "internal").mkdir(parents=True)
    (src / "build.sh").write_text(BUILD_SH)
    (src / "VERSION").write_text("1.0.0")
    (src / "main.go").write_text("package main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    go = bin_dir / "go"
    go.write_text("#!/bin/sh\n")
    go.chmod(go.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings.DEVICE_AGENT_SOURCE = src
    settings.DEVICE_AGENT_DIR = src / "dist"
    settings.DEVICE_AGENT_AUTOBUILD = True
    monkeypatch.setattr(agentbuild, "CHECK_EVERY", 0)
    monkeypatch.setattr(agentbuild, "_failed_for", None)
    monkeypatch.setattr(agentbuild, "_building", None)
    return src


def touch_later(path, text):
    path.write_text(text)
    later = time.time() + 5
    os.utime(path, (later, later))


def builds(src):
    path = src / "builds"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_missing_or_stale_builds_are_rebuilt(source):
    agentbuild.wait_until_fresh()
    assert (source / "dist" / "nexguard-darwin-arm64").read_text() == "1.0.0"
    agentbuild.wait_until_fresh()
    assert builds(source) == 1, "fresh builds aren't rebuilt"

    touch_later(source / "internal" / "relay.go", "package relay")
    touch_later(source / "VERSION", "1.1.0")
    agentbuild.wait_until_fresh()
    assert (source / "dist" / "nexguard-darwin-arm64").read_text() == "1.1.0"
    assert builds(source) == 2
    assert not list(source.glob(".dist-*")), "no build folders left behind"


def test_a_failed_build_keeps_the_old_one_and_is_not_retried(source):
    agentbuild.wait_until_fresh()
    (source / "fail").write_text("")
    touch_later(source / "main.go", "package main // broken")
    agentbuild.wait_until_fresh()
    agentbuild.wait_until_fresh()
    assert (source / "dist" / "nexguard-darwin-arm64").read_text() == "1.0.0"
    assert builds(source) == 1


@pytest.mark.parametrize("why", ["off", "own folder", "no go"])
def test_only_the_builds_next_to_the_source_where_go_exists(source, settings, monkeypatch, why):
    if why == "off":
        settings.DEVICE_AGENT_AUTOBUILD = False
    elif why == "own folder":
        settings.DEVICE_AGENT_DIR = source.parent / "elsewhere"
    else:
        monkeypatch.setenv("PATH", "/nonexistent")
    agentbuild.wait_until_fresh()
    assert builds(source) == 0


@pytest.mark.django_db
def test_the_install_script_starts_the_build(source, client):
    response = client.get("/d/K7PQ3MXA/install.sh")
    assert response.status_code == 200
    response = client.get("/d/agent/SHA256SUMS")  # waits for that build
    assert b"nexguard-darwin-arm64" in b"".join(response.streaming_content)
    assert builds(source) == 1
