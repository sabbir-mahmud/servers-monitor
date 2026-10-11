"""3.4: agents as uptime probes (HTTP and TCP checks assigned by the dashboard)."""

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_monitor_agent as sma  # noqa: E402
from test_agent import FakeRelay, write_config  # noqa: E402

MID = "0192f0c4-7a1e-7c3e-9c1a-2b3c4d5e6f70"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        body = b"status: ok" if self.path == "/health" else b"Maintenance"
        self.send_response(200 if self.path in ("/health", "/maint") else 503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def web():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def http_spec(url, **extra):
    return sma.clean_probe(
        {
            "id": MID,
            "kind": "http",
            "url": url,
            "method": "GET",
            "expected": "200-399",
            "interval": 60,
            "timeout": 5,
            **extra,
        }  # fmt: skip
    )


@pytest.fixture
def probe_cfg(tmp_path):
    return sma.load_config(write_config(tmp_path, "[probes]\nenabled = true\n"))


def test_probes_are_opt_in(tmp_path, probe_cfg):
    off = sma.load_config(write_config(tmp_path))
    assert "probes" not in sma.Collector(off).capabilities()["features"]
    assert sma.Agent(off).prober is None
    assert "probes" in sma.Collector(probe_cfg).capabilities()["features"]


def test_internal_urls_allowed_but_metadata_never(web):
    assert sma.run_probe(http_spec(f"{web}/health"))["ok"]  # loopback: the point of agent probes
    for url in ("http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://0.0.0.0/"):
        result = sma.run_probe(http_spec(url))
        assert not result["ok"] and "never contact" in result["error"]


def test_http_status_keyword_and_redirects(web):
    ok = sma.run_probe(http_spec(f"{web}/health", keyword="ok"))
    assert ok["ok"] and ok["code"] == 200 and ok["id"] == MID
    assert sma.run_probe(http_spec(f"{web}/missing"))["error"] == "HTTP 503"
    absent = sma.run_probe(http_spec(f"{web}/maint", keyword="Maintenance", keyword_absent=True))
    assert not absent["ok"] and "appeared" in absent["error"]
    assert sma.run_probe(http_spec(f"{web}/moved"))["code"] == 200
    assert sma.run_probe(http_spec(f"{web}/moved", follow_redirects=False))["code"] == 302


def test_tcp_open_and_closed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        spec = sma.clean_probe({"id": MID, "kind": "tcp", "host": "127.0.0.1", "port": port})
        assert sma.run_probe(spec)["ok"]
    closed = sma.run_probe(spec)
    assert not closed["ok"] and closed["error"] == "connection refused"


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x", "kind": "http", "url": "http://a/", "method": "GET"},  # id not a uuid
        {"id": MID, "kind": "http", "url": "file:///etc/passwd", "method": "GET"},
        {"id": MID, "kind": "http", "url": "http://a/", "method": "DELETE"},
        {"id": MID, "kind": "http", "url": "http://a/", "method": "GET", "expected": "2xx"},
        {"id": MID, "kind": "tcp", "host": "a", "port": 70000},
        {"id": MID, "kind": "ping", "host": "a"},
    ],
)
def test_bad_assignments_are_dropped(bad):
    assert sma.clean_probe(bad) is None


def test_results_ride_on_heartbeats_and_survive_a_failed_send(probe_cfg, web, monkeypatch):
    agent = sma.Agent(probe_cfg)
    relay = FakeRelay()
    agent.relay = relay
    monkeypatch.setattr(agent.collector, "report", lambda: {"agent_version": sma.VERSION})
    agent.handle({"t": "cfg", "report_interval": 120, "probes_rev": "r1",
                  "probes": [{"id": MID, "kind": "http", "url": f"{web}/health", "method": "GET"}]})  # fmt: skip
    assert agent.prober.run_due() == 1 and agent.prober.run_due() == 0  # once per interval

    def down(message):
        raise sma.RedisError("relay down")

    relay.send = down
    assert not agent.heartbeat()
    relay.send = FakeRelay.send.__get__(relay)
    assert agent.heartbeat()
    report = relay.sent[-1]["report"]
    assert report["probes_rev"] == "r1"
    assert [r["id"] for r in report["probe_results"]] == [MID]  # kept for the next heartbeat
    assert agent.heartbeat() and relay.sent[-1]["report"]["probe_results"] == []


def test_reassigning_drops_old_probes(probe_cfg):
    prober = sma.Prober(lambda: 120)
    prober.assign([{"id": MID, "kind": "tcp", "host": "127.0.0.1", "port": 9}], "a")
    prober.results[MID] = [{"id": MID, "ok": True}]
    prober.assign([], "b")
    assert prober.specs == [] and prober.take() == [] and prober.rev == "b"
