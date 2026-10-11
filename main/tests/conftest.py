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


class AgentClient:
    def __init__(self, client, token):
        self.client, self.auth = client, {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def heartbeat(self, body=None):
        return self.client.post(
            "/api/agent/v1/heartbeat/",
            json.dumps(body or report()),
            content_type="application/json",
            **self.auth,
        )

    def poll(self):
        return self.client.get("/api/agent/v1/commands/", **self.auth)

    def result(self, command_id, **body):
        return self.client.post(
            f"/api/agent/v1/commands/{command_id}/",
            json.dumps(body),
            content_type="application/json",
            **self.auth,
        )


@pytest.fixture
def agent(client, server_and_token):
    return AgentClient(client, server_and_token[1])
