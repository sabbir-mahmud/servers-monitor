"""The reference device client for computers (agent/nexguard_device.py)."""

import json
import socketserver
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nexguard_device as vd  # noqa: E402


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    path = tmp_path / "nexguard" / "device.json"
    monkeypatch.setenv("NEXGUARD_DEVICE_CONFIG", str(path))
    return path


class FakeDashboard:
    """Answers pairing requests the way the bridge does (apps/agent_api/bridge.py)."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, relay_url, payload):
        self.calls.append((relay_url, payload))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


RELAY = "rediss://sm-pc:pw@relay.example.com:6379/?prefix=sm"
DEVICE = {"name": "Sara's laptop", "account": "The Rahmans"}
DISCLOSURE = "The Rahmans will see this device's last online time…"
LOGIN = {"url": "rediss://sm-d-1:pw@relay.example.com:6379", "inbox": "sm:d:1:in",
         "config": "sm:d:1:cfg", "blocklist": "sm:d:1:bl"}  # fmt: skip
PAIRED = {"state": "paired", "relay": LOGIN, "device": DEVICE, "checkin_seconds": 300,
          "disclosure": DISCLOSURE}  # fmt: skip


def test_pair_asks_first_and_stores_the_login_privately(config):
    dashboard = FakeDashboard({"state": "preview", "disclosure": DISCLOSURE, "device": DEVICE},
                              dict(PAIRED))  # fmt: skip
    seen = []
    data = vd.pair(RELAY, "K7PQ-3MXA", accept=lambda d: seen.append(d) or True, ask=dashboard)
    # First a preview (the code stays valid), and only after a yes the real pairing.
    (url, asked), (_, paired) = dashboard.calls
    assert url == RELAY and asked["code"] == "K7PQ-3MXA" and asked["preview"] is True
    assert "secret" not in asked and "preview" not in paired and len(paired["secret"]) == 64
    assert paired["info"]["platform"] in ("macos", "windows", "linux") and seen == [DISCLOSURE]
    stored = vd.load_config()
    assert data["relay"] == LOGIN and stored["key"] == paired["secret"]
    if sys.platform != "win32":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_declining_stores_nothing_and_never_pairs(config):
    dashboard = FakeDashboard({"state": "preview", "disclosure": DISCLOSURE, "device": DEVICE})
    with pytest.raises(vd.ClientError, match="declined"):
        vd.pair(RELAY, "K7PQ-3MXA", accept=lambda d: False, ask=dashboard)
    assert not config.exists()
    assert len(dashboard.calls) == 1 and dashboard.calls[0][1]["preview"] is True  # code kept


def test_plain_redis_only_on_this_machine_or_the_lan(monkeypatch):
    assert vd.check_relay("redis://u:p@127.0.0.1:6379")
    assert vd.check_relay("redis://u:p@192.168.1.20:6379")
    for bad in ("redis://u:p@relay.example.com:6379", "https://nexguard.example.com"):
        with pytest.raises(vd.ClientError):
            vd.check_relay(bad)
    monkeypatch.setenv("NEXGUARD_ALLOW_PLAIN", "1")
    assert vd.check_relay("redis://u:p@relay.example.com:6379")


def test_run_checks_in_and_backs_off_on_errors():
    replies = [None, vd.ClientError("down"), vd.Unpaired("gone")]

    def send(config):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        config["checkin_seconds"] = 900  # what the dashboard's settings said

    vd.save_config({"relay": LOGIN, "key": "ab" * 32, "checkin_seconds": 300})
    sleeps = []
    code = vd.run(vd.load_config(), sleep=sleeps.append, send=send)
    assert sleeps == [900, 1800]  # interval from the dashboard, then doubled (capped) on error
    assert code == 3 and vd.load_config() is None  # unpaired: forget, ask to pair again


class TinyRelay(socketserver.ThreadingTCPServer):
    """Just enough Redis: one login, RPUSH and GET; a wrong password gets WRONGPASS."""

    allow_reuse_address = daemon_threads = True

    def __init__(self):
        self.lists, self.strings, self.password = {}, {}, "pw"
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while line := self.rfile.readline():
                    args = []
                    for _ in range(int(line[1:])):
                        size = int(self.rfile.readline()[1:])
                        args.append(self.rfile.read(size + 2)[:-2].decode())
                    cmd = args[0].upper()
                    if cmd == "AUTH":
                        ok = args[-1] == server.password
                        self.wfile.write(b"+OK\r\n" if ok else b"-WRONGPASS invalid\r\n")
                    elif cmd == "RPUSH":
                        server.lists.setdefault(args[1], []).append(args[2])
                        self.wfile.write(b":1\r\n")
                    elif cmd == "GET":
                        value = server.strings.get(args[1])
                        data = value.encode() if value is not None else None
                        self.wfile.write(b"$-1\r\n" if data is None else
                                         b"$%d\r\n%s\r\n" % (len(data), data))  # fmt: skip

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


def test_checkin_is_signed_and_reads_the_signed_settings():
    relay = TinyRelay()
    key = bytes(range(32))
    port = relay.server_address[1]
    login = {"url": f"redis://sm-d-1:pw@127.0.0.1:{port}", "inbox": "sm:d:1:in",
             "config": "sm:d:1:cfg"}  # fmt: skip
    settings = json.dumps({"checkin_seconds": 600, "controls": None})
    relay.strings["sm:d:1:cfg"] = json.dumps({"body": settings, "sig": vd.sign(key, settings)})
    config = {"relay": login, "key": key.hex(), "checkin_seconds": 300}
    vd.checkin(config)
    message = json.loads(relay.lists["sm:d:1:in"][0])
    assert message["t"] == "ci" and message["sig"] == vd.sign(key, message["body"])
    assert "storage" in json.loads(message["body"])["report"] and config["checkin_seconds"] == 600

    forged = json.dumps({"checkin_seconds": 60})
    relay.strings["sm:d:1:cfg"] = json.dumps({"body": forged, "sig": "0" * 64})
    vd.checkin(config)
    assert config["checkin_seconds"] == 600  # not signed with its key: ignored

    relay.password = "changed"  # the dashboard removed it (or paired it again)
    with pytest.raises(vd.Unpaired):
        vd.checkin(config)
    relay.shutdown()


def test_report_has_the_basics():
    data = vd.report()
    assert data["platform"] == vd.this_platform() and data["os_version"]
    assert data["storage"]["total"] >= data["storage"]["free"] > 0


def test_reverse_dns_host_names_are_shortened_only_when_meaningful():
    import nexguard_device as n

    assert n.short_hostname("sara-mbp.local") == "sara-mbp"
    assert n.short_hostname("5-1-1-3.ipv4.isp.example") == "5-1-1-3.ipv4.isp.example"
    assert n.short_hostname("desktop") == "desktop"
    assert n.model()  # never empty: falls back to the CPU architecture
