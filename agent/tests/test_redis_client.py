"""The agent's built-in RESP client, against a tiny in-process Redis-speaking server."""

import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_monitor_agent as sma  # noqa: E402


class MiniRedis(threading.Thread):
    """Accepts one connection; answers AUTH/RPUSH/BLPOP/LTRIM/PING from a dict of lists."""

    def __init__(self, password="pw"):
        super().__init__(daemon=True)
        self.password, self.lists, self.seen = password, {}, []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]

    def run(self):
        conn, _ = self.server.accept()
        f = conn.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                return
            args = []
            for _ in range(int(line[1:])):
                n = int(f.readline()[1:])
                args.append(f.read(n + 2)[:-2].decode())
            self.seen.append(args)
            conn.sendall(self.reply(args))

    def reply(self, args) -> bytes:
        cmd = args[0].upper()
        if cmd == "AUTH":
            return b"+OK\r\n" if args[-1] == self.password else b"-WRONGPASS invalid password\r\n"
        if cmd == "PING":
            return b"+PONG\r\n"
        if cmd == "RPUSH":
            self.lists.setdefault(args[1], []).extend(args[2:])
            return b":%d\r\n" % len(self.lists[args[1]])
        if cmd == "LTRIM":
            return b"+OK\r\n"
        if cmd == "BLPOP":
            items = self.lists.get(args[1])
            if not items:
                return b"*-1\r\n"
            value = items.pop(0).encode()
            key = args[1].encode()
            return b"*2\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n" % (len(key), key, len(value), value)
        return b"-ERR unknown command\r\n"


def test_client_auth_push_pop_and_errors():
    srv = MiniRedis()
    srv.start()
    r = sma.Redis(f"redis://sm-x:pw@127.0.0.1:{srv.port}")
    assert r.call("PING") == "PONG"
    assert srv.seen[0] == ["AUTH", "sm-x", "pw"]
    assert r.call("RPUSH", "k", "ünïcode ✓") == 1
    assert r.call("BLPOP", "k", 1, block=1) == ["k", "ünïcode ✓"]
    assert r.call("BLPOP", "k", 1, block=1) is None
    with pytest.raises(sma.RedisError, match="unknown"):
        r.call("FLUSHALL")
    r.close()


def test_wrong_password_raises_redis_error():
    srv = MiniRedis(password="right")
    srv.start()
    with pytest.raises(sma.RedisError, match="WRONGPASS"):
        sma.Redis(f"redis://sm-x:wrong@127.0.0.1:{srv.port}").call("PING")


def test_relay_uses_the_servers_own_keys(tmp_path):
    srv = MiniRedis()
    srv.start()
    conf = tmp_path / "a.toml"
    conf.write_text(
        f'[main]\nredis_url = "redis://sm-abc:pw@127.0.0.1:{srv.port}"\nallow_insecure = true\n'
    )
    relay = sma.Relay(sma.load_config(conf))
    relay.reader = relay.writer  # MiniRedis serves a single connection
    relay.send({"t": "hb"})
    assert srv.lists == {"sm:a:abc:in": ['{"t":"hb"}']}
    srv.lists["sm:a:abc:cmd"] = ['{"t":"cfg","report_interval":60}']
    assert relay.receive(1) == {"t": "cfg", "report_interval": 60}
