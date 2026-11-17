"""The reference device client for computers (agent/nexguard_device.py)."""

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nexguard_device as vd  # noqa: E402


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    path = tmp_path / "nexguard" / "device.json"
    monkeypatch.setenv("NEXGUARD_DEVICE_CONFIG", str(path))
    return path


class FakeServer:
    def __init__(self, monkeypatch):
        self.calls, self.replies = [], []
        monkeypatch.setattr(vd, "post", self)

    def __call__(self, url, payload, token=None):
        self.calls.append((url, payload, token))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


PAIRED = {
    "token": "ngd_abc",
    "device": {"name": "Sara's laptop", "account": "The Rahmans"},
    "checkin_seconds": 300,
    "disclosure": "The Rahmans will see this device's last online time…",
}


def test_pair_asks_first_and_stores_the_token_privately(monkeypatch, config):
    server = FakeServer(monkeypatch)
    server.replies = [dict(PAIRED)]
    seen = []
    data = vd.pair("nexguard.example.com", "K7PQ-3MXA", accept=lambda d: seen.append(d) or True)
    url, payload, _ = server.calls[0]
    assert url == "https://nexguard.example.com/device/v1/pair" and payload["code"] == "K7PQ-3MXA"
    assert payload["platform"] in ("macos", "windows", "linux") and seen == [PAIRED["disclosure"]]
    assert (
        data["token"] == "ngd_abc" and vd.load_config()["server"] == "https://nexguard.example.com"
    )
    if sys.platform != "win32":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_declining_stores_nothing(monkeypatch, config):
    FakeServer(monkeypatch).replies = [dict(PAIRED)]
    with pytest.raises(vd.ClientError, match="declined"):
        vd.pair("https://nexguard.example.com", "K7PQ-3MXA", accept=lambda d: False)
    assert not config.exists()


def test_plain_http_only_on_this_machine_or_the_lan(monkeypatch):
    assert vd.check_server("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert vd.check_server("http://192.168.1.20:8000") == "http://192.168.1.20:8000"
    with pytest.raises(vd.ClientError, match="https"):
        vd.check_server("http://nexguard.example.com")
    monkeypatch.setenv("NEXGUARD_ALLOW_HTTP", "1")
    assert vd.check_server("http://nexguard.example.com")


def test_run_checks_in_and_backs_off_on_errors(monkeypatch):
    server = FakeServer(monkeypatch)
    server.replies = [{"checkin_seconds": 900}, vd.ClientError("down"), vd.Unpaired("gone")]
    vd.save_config({"server": "https://v.test", "token": "ngd_abc", "checkin_seconds": 300})
    sleeps = []
    code = vd.run(vd.load_config(), sleep=sleeps.append)
    assert sleeps == [900, 1800]  # interval from the server, then doubled (capped) on error
    assert server.calls[0][2] == "ngd_abc" and "storage" in server.calls[0][1]
    assert code == 3 and vd.load_config() is None  # unpaired: forget, ask to pair again


def test_report_has_the_basics():
    data = vd.report()
    assert data["platform"] == vd.this_platform() and data["os_version"]
    assert data["storage"]["total"] >= data["storage"]["free"] > 0
