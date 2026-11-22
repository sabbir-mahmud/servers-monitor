"""A real Redis-protocol server on 127.0.0.1 for end-to-end tests, backed by the tests'
FakeRedis (conftest.py). Logins and key patterns follow the ACL users the dashboard made
there, and every RPUSH is handed to the bridge at once, as `manage.py relay` would do, so
a real agent can pair and check in against a live dashboard."""

import fnmatch
import json
import socketserver
import threading

from django.db import close_old_connections

from apps.agent_api import relay
from apps.agent_api.bridge import handle_device, handle_pairing
from apps.devices.models import Device


def _bulk(value: str | None) -> bytes:
    if value is None:
        return b"$-1\r\n"
    data = value.encode()
    return b"$%d\r\n%s\r\n" % (len(data), data)


class _Handler(socketserver.StreamRequestHandler):
    def _command(self) -> list[str] | None:
        line = self.rfile.readline()
        if not line.startswith(b"*"):
            return None
        args = []
        for _ in range(int(line[1:])):
            size = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(size + 2)[:-2].decode())
        return args

    def handle(self):
        fake, user = self.server.fake, None
        while (args := self._command()) is not None:
            cmd = args[0].upper()
            if cmd == "AUTH":
                name, password = (args[1], args[2]) if len(args) == 3 else ("default", args[1])
                if f">{password}" in fake.users.get(name, []):
                    user = name
                    self.wfile.write(b"+OK\r\n")
                else:
                    self.wfile.write(b"-WRONGPASS invalid username-password pair\r\n")
                continue
            if cmd == "PING":
                self.wfile.write(b"+PONG\r\n")
                continue
            rules = fake.users.get(user or "", [])
            allowed = [r[1:] for r in rules if str(r).startswith("~")]
            if f"+{cmd.lower()}" not in rules or not any(
                fnmatch.fnmatchcase(args[1], p) for p in allowed
            ):
                self.wfile.write(b"-NOPERM this user has no permissions\r\n")
                continue
            if cmd == "GET":
                self.wfile.write(_bulk(fake.strings.get(args[1])))
            elif cmd == "RPUSH":
                # Handled before the reply, so the agent's GET right after sees the result
                # (the real bridge races it, and the agent picks it up a check-in later).
                self.server.deliver(args[1], args[2])
                self.wfile.write(b":1\r\n")
            else:
                self.wfile.write(b"-ERR unknown command\r\n")


class RespServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, fake):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fake = fake
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def deliver(self, key: str, raw: str) -> None:
        """What the bridge's BLPOP would have received."""
        with self.lock:
            close_old_connections()
            message = json.loads(raw)
            if key == relay.pair_inbox_key():
                handle_pairing(message)
                return
            device_id = key.split(":")[-2]
            device = (
                Device.objects.select_related("account", "person", "created_by")
                .filter(pk=device_id)
                .first()
            )
            if device is not None:
                handle_device(device, message)

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
