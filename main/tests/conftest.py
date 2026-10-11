import json

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache

from apps.fleet.models import Server
from apps.fleet.tokens import generate_token
from apps.users.roles import sync_roles


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _no_forced_2fa(settings):
    """Most tests sign in as an Admin; 2FA enforcement has its own tests (test_two_factor)."""
    settings.REQUIRE_2FA_FOR_ADMINS = False


@pytest.fixture
def roles(db):
    sync_roles()


def _user(django_user_model, name, role):
    user = django_user_model.objects.create_user(name, password="pw-not-used-1!")
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def viewer(django_user_model, roles):
    return _user(django_user_model, "viewer", "Viewer")


@pytest.fixture
def operator(django_user_model, roles):
    return _user(django_user_model, "operator", "Operator")


@pytest.fixture
def admin_user(django_user_model, roles):
    return _user(django_user_model, "admin", "Admin")


def make_server(name="web-01", **kwargs):
    raw, digest, prefix = generate_token()
    server = Server.objects.create(name=name, token_hash=digest, token_prefix=prefix, **kwargs)
    return server, raw


@pytest.fixture
def server_and_token(db):
    return make_server()


@pytest.fixture
def server(db):
    return make_server()[0]


def report(**overrides):
    body = {
        "agent_version": "1.0.0",
        "host": {
            "hostname": "web-01",
            "platform": "Linux",
            "cpu_count": 4,
            "memory_total": 8 * 2**30,
            "uptime_seconds": 3600,
        },
        "metrics": {
            "cpu_percent": 12.5,
            "memory_percent": 40.0,
            "disk_percent": 50.0,
            "load_1": 0.4,
            "net_rx_bps": 1000.0,
            "net_tx_bps": 500.0,
        },
        "disks": [{"mount": "/", "total": 100, "used": 50, "percent": 50.0}],
        "services": [
            {
                "name": "nginx",
                "kind": "systemd",
                "state": "up",
                "status": "active",
                "version": "nginx/1.24",
                "restartable": True,
            }
        ],
        "capabilities": {
            "actions": [{"name": "deploy_api", "description": "Deploy", "timeout": 600}],
            "logs": ["nginx_error"],
            "shell": False,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return body


class FakeRedis:
    """In-memory stand-in for the relay: the list and ACL commands the dashboard uses, with
    Upstash's quirk that ACL passwords must come from ACL GENTOKEN."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.users: dict[str, list] = {}
        self.tokens: set[str] = set()
        self.down = False

    def _check(self):
        if self.down:
            import redis

            raise redis.ConnectionError("relay unreachable (test)")

    def execute_command(self, *args):
        self._check()
        op = [str(a).upper() for a in args[:2]]
        if op == ["ACL", "GENTOKEN"]:
            token = f"tok_{args[2]}_{len(self.tokens)}"
            self.tokens.add(token)
            return token
        if op == ["ACL", "SETUSER"]:
            rules = list(args[3:])
            for rule in rules:
                if str(rule).startswith(">") and rule[1:] not in self.tokens:
                    import redis

                    raise redis.ResponseError("The password must be generated with ACL GENTOKEN")
            self.users[args[2]] = rules if "reset" in rules else self.users.get(args[2], []) + rules
            return "OK"
        if op == ["ACL", "DELUSER"]:
            return int(self.users.pop(args[2], None) is not None)
        raise NotImplementedError(args)

    def rpush(self, key, *values):
        self._check()
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lrem(self, key, count, value):
        self._check()
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0

    def blpop(self, keys, timeout=0):
        self._check()
        for key in keys:
            if self.lists.get(key):
                return key, self.lists[key].pop(0)
        return None

    def delete(self, *keys):
        return sum(self.lists.pop(k, None) is not None for k in keys)

    def drain(self, key) -> list[dict]:
        items, self.lists[key] = self.lists.get(key, []), []
        return [json.loads(i) for i in items]


@pytest.fixture(autouse=True)
def fake_relay(monkeypatch, settings):
    from apps.agent_api import relay

    fake = FakeRedis()
    settings.REDIS_URL = "rediss://default:admin-secret@relay.test:6379"
    monkeypatch.setattr(relay, "client", lambda: fake)
    return fake


class AgentClient:
    """Plays one agent: puts messages in its relay inbox the way the real agent does and
    runs them through the bridge, and reads what the dashboard pushed to its mailbox."""

    def __init__(self, server, fake):
        self.server, self.fake = server, fake

    def _send(self, message):
        from apps.agent_api import signing
        from apps.agent_api.bridge import handle

        self.server.refresh_from_db()
        if key := signing.server_key(self.server):
            message = signing.sign(message, key)  # what a real, keyed agent does
        handle(self.server, message)

    def heartbeat(self, body=None, interval=None):
        from datetime import timedelta

        self.server.refresh_from_db()
        # Real heartbeats are an interval apart; age the last one so the bridge's
        # too-soon guard doesn't discard back-to-back test heartbeats.
        if self.server.last_seen_at:
            Server.objects.filter(pk=self.server.pk).update(
                last_seen_at=self.server.last_seen_at
                - timedelta(seconds=self.server.report_interval)
            )
        interval = self.server.report_interval if interval is None else interval
        self._send({"t": "hb", "interval": interval, "report": body or report()})

    def mailbox(self) -> list[dict]:
        """BLPOP everything queued for this agent (each message is seen once)."""
        from apps.agent_api import relay

        return self.fake.drain(relay.keys(self.server)[0])

    def ack(self, command_id):
        self._send({"t": "ack", "id": str(command_id)})

    def result(self, command_id, **body):
        self._send({"t": "res", "id": str(command_id), **body})


@pytest.fixture
def agent(server_and_token, fake_relay):
    return AgentClient(server_and_token[0], fake_relay)
