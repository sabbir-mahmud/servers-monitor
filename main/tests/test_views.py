import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.fleet.models import Server
from apps.fleet.tokens import hash_token

from .conftest import make_server

pytestmark = pytest.mark.django_db


def test_anonymous_redirected_to_login(client):
    resp = client.get(reverse("fleet:dashboard"))
    assert resp.status_code == 302 and "/login/" in resp["Location"]


def test_htmx_poll_with_expired_session_redirects_window(client):
    resp = client.get(reverse("fleet:dashboard"), HTTP_HX_REQUEST="true")
    assert resp.status_code == 204 and resp["HX-Redirect"] == "/login/"


@pytest.mark.parametrize(
    "url_name,role,expected",
    [
        ("fleet:dashboard", "viewer", 200),
        ("fleet:server_create", "viewer", 403),
        ("fleet:server_create", "operator", 403),
        ("fleet:server_create", "admin_user", 200),
        ("operations:job_create", "viewer", 403),
        ("operations:job_create", "operator", 200),
        ("audit:list", "operator", 403),
        ("audit:list", "admin_user", 200),
        ("alerts:list", "viewer", 200),
    ],
)
def test_role_matrix(client, request, url_name, role, expected):
    client.force_login(request.getfixturevalue(role))
    assert client.get(reverse(url_name)).status_code == expected


def test_dashboard_query_budget(client, viewer, django_assert_max_num_queries):
    for i in range(20):
        make_server(f"s{i:02}")
    client.force_login(viewer)
    # Constant regardless of server count: servers + groups prefetch + groups + alerts + session/user.
    with django_assert_max_num_queries(8):
        resp = client.get(reverse("fleet:dashboard"), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and b"s19" in resp.content
    assert b"<html" not in resp.content  # partial only


def test_create_server_shows_credentials_once_and_stores_hash(client, admin_user, fake_relay):
    client.force_login(admin_user)
    resp = client.post(
        reverse("fleet:server_create"),
        {
            "name": "api-01",
            "environment": "production",
            "report_interval": 120,
            "is_enabled": "on",
            "new_groups": "api, Workers",
        },
    )
    server = Server.objects.get(name="api-01")
    assert resp["Location"] == reverse("fleet:server_enroll", args=[server.pk])
    page = client.get(resp["Location"]).content.decode()
    agent_url = page.split('id="token" class="fs-6 text-break">')[1].split("<")[0]
    password = agent_url.split(":")[2].split("@")[0]
    assert agent_url.startswith(f"rediss://sm-{server.pk}:") and "raw.githubusercontent.com" in page
    assert server.token_hash == hash_token(password) and password not in server.token_hash
    assert f"sm-{server.pk}" in fake_relay.users
    assert sorted(server.groups.values_list("name", flat=True)) == ["api", "workers"]
    assert (
        password
        not in client.get(reverse("fleet:server_enroll", args=[server.pk])).content.decode()
    )
    assert AuditLog.objects.filter(action="server.create", server=server).exists()


def test_rotate_credentials_replaces_relay_password(
    client, admin_user, server_and_token, fake_relay
):
    from apps.agent_api import relay

    server, _ = server_and_token
    relay.provision(server)
    old_rules = fake_relay.users[f"sm-{server.pk}"]
    client.force_login(admin_user)
    resp = client.post(reverse("fleet:server_rotate_token", args=[server.pk]))
    assert resp["Location"] == reverse("fleet:server_enroll", args=[server.pk])
    new_rules = fake_relay.users[f"sm-{server.pk}"]
    # `reset` first: the old password is dropped, only the new one is accepted.
    assert new_rules[0] == "reset" and new_rules != old_rules
    assert AuditLog.objects.filter(action="server.rotate_token", server=server).exists()


def test_server_detail_renders_with_data(client, operator, agent, server_and_token):
    agent.heartbeat()
    client.force_login(operator)
    resp = client.get(reverse("fleet:server_detail", args=[server_and_token[0].pk]))
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "deploy_api" in html and "nginx_error" in html and 'class="spark"' in html
    part = client.get(
        reverse("fleet:server_detail", args=[server_and_token[0].pk]) + "?part=live",
        HTTP_HX_REQUEST="true",
    )
    assert b'id="live"' in part.content and b"<html" not in part.content


def test_inline_action_returns_command_partial(client, operator, agent, server_and_token):
    agent.heartbeat()
    client.force_login(operator)
    resp = client.post(
        reverse("operations:job_create"),
        {"action": "deploy_api", "servers": [server_and_token[0].pk]},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and b"Queued" in resp.content


def test_bulk_job_redirects_to_detail(client, operator, agent, server_and_token):
    agent.heartbeat()
    client.force_login(operator)
    resp = client.post(
        reverse("operations:job_create"),
        {"action": "deploy_api", "servers": [server_and_token[0].pk]},
    )
    assert resp.status_code == 302
    assert client.get(resp["Location"]).status_code == 200


def test_login_throttle(client, viewer, settings):
    settings.LOGIN_FAILURE_LIMIT = 2
    for _ in range(2):
        client.post(reverse("login"), {"username": "viewer", "password": "wrong"})
    resp = client.post(reverse("login"), {"username": "viewer", "password": "pw-not-used-1!"})
    assert b"Too many failed attempts" in resp.content


def test_no_agent_facing_http_endpoints(client):
    """Agents talk only to the relay: the dashboard exposes nothing for them."""
    assert client.get("/agent/files/install.sh").status_code == 404
    assert (
        client.post("/api/agent/v1/heartbeat/", "{}", content_type="application/json").status_code
        == 404
    )
