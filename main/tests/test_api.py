"""4.1: REST API tokens and the Prometheus export."""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind, Severity
from apps.api.models import ApiToken, Scope
from apps.audit.models import AuditLog
from apps.monitors.models import Monitor, MonitorStatus
from apps.operations.models import Job

from .conftest import AgentClient, make_server, report

pytestmark = pytest.mark.django_db


def bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


@pytest.fixture
def read_token(operator):
    return ApiToken.issue(operator, "grafana", Scope.READ)[1]


@pytest.fixture
def operate_token(operator):
    return ApiToken.issue(operator, "deploy-bot", Scope.OPERATE)[1]


@pytest.fixture
def online(agent):
    agent.heartbeat()
    return agent.server


def test_requires_a_valid_token(client, read_token, operator):
    url = reverse("api_servers")
    assert client.get(url).status_code == 401
    client.force_login(operator)  # a browser session is not an API credential
    assert client.get(url).status_code == 401
    assert client.get(url, **bearer("smt_guess")).status_code == 401
    assert client.get(url, **bearer(read_token)).status_code == 200


def test_revoked_expired_and_deactivated_tokens_stop_working(client, operator):
    token, raw = ApiToken.issue(operator, "t", Scope.READ)
    url = reverse("api_servers")
    ApiToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get(url, **bearer(raw)).status_code == 401
    ApiToken.objects.filter(pk=token.pk).update(expires_at=None, revoked_at=timezone.now())
    assert client.get(url, **bearer(raw)).status_code == 401
    ApiToken.objects.filter(pk=token.pk).update(revoked_at=None)
    operator.is_active = False
    operator.save()
    assert client.get(url, **bearer(raw)).status_code == 401


def test_only_a_hash_is_stored(operator):
    token, raw = ApiToken.issue(operator, "t", Scope.READ)
    assert raw.startswith("smt_") and raw not in json.dumps(
        list(ApiToken.objects.values()), default=str
    )
    assert raw.startswith(token.prefix)


def test_rate_limited_per_token(client, read_token, settings):
    settings.API_RATE_LIMIT = 3
    codes = [client.get(reverse("api_servers"), **bearer(read_token)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_bad_token_guessing_is_throttled(client):
    codes = {
        client.get(reverse("api_servers"), **bearer(f"smt_{i}")).status_code for i in range(35)
    }
    assert codes == {401, 429}


def test_servers_list_and_detail(client, read_token, online):
    body = client.get(reverse("api_servers"), **bearer(read_token)).json()
    (row,) = body["results"]
    assert row["name"] == "web-01" and row["health"] == "online" and body["next_offset"] is None
    assert row["metrics"]["cpu_percent"] == 12.5
    detail = client.get(reverse("api_server", args=[online.pk]), **bearer(read_token)).json()
    assert detail["actions"][0]["name"] == "deploy_api" and detail["services"][0]["name"] == "nginx"


def test_pagination(client, read_token):
    for i in range(3):
        make_server(f"s-{i}")
    page = client.get(reverse("api_servers") + "?limit=2", **bearer(read_token)).json()
    assert [r["name"] for r in page["results"]] == ["s-0", "s-1"] and page["next_offset"] == 2


def test_monitors_and_alerts(client, read_token):
    m = Monitor.objects.create(name="site", url="https://example.com", status=MonitorStatus.DOWN)
    Alert.objects.create(
        monitor=m, kind=AlertKind.APP_DOWN, severity=Severity.CRITICAL, message="site is down"
    )
    mons = client.get(reverse("api_monitors") + "?status=down", **bearer(read_token)).json()
    assert [r["name"] for r in mons["results"]] == ["site"]
    alerts = client.get(reverse("api_alerts"), **bearer(read_token)).json()["results"]
    assert alerts[0]["message"] == "site is down" and alerts[0]["monitor"] == str(m.pk)


def test_read_token_cannot_run_jobs(client, read_token, online):
    body = json.dumps({"action": "deploy_api", "servers": ["web-01"]})
    r = client.post(
        reverse("api_jobs"), body, content_type="application/json", **bearer(read_token)
    )
    assert r.status_code == 403 and not Job.objects.exists()


def test_operate_token_runs_an_allowlisted_action_and_is_audited(
    client, operate_token, online, agent, django_capture_on_commit_callbacks
):
    body = json.dumps({"action": "deploy_api", "servers": [str(online.pk)], "note": "from CI"})
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            reverse("api_jobs"), body, content_type="application/json", **bearer(operate_token)
        )
    assert r.status_code == 201, r.content
    job = Job.objects.get()
    assert r.json()["id"] == str(job.pk) and job.note == "from CI"
    assert [m["action"] for m in agent.mailbox() if m["t"] == "cmd"] == ["deploy_api"]
    entry = AuditLog.objects.get(action="job.create")
    assert entry.actor_label == "operator" and entry.data["via"].startswith("API token deploy-bot")
    detail = client.get(reverse("api_job", args=[job.pk]), **bearer(operate_token)).json()
    assert detail["commands"][0]["server_name"] == "web-01"


def test_api_cannot_run_shell_or_unknown_servers(client, admin_user, online):
    raw = ApiToken.issue(admin_user, "admin-bot", Scope.OPERATE)[1]
    for body, code in (({"action": "shell", "args": {"command": "id"}, "servers": ["web-01"]}, 403),
                       ({"action": "deploy_api", "servers": ["nope"]}, 400),
                       ({"action": "not_offered", "servers": ["web-01"]}, 400)):  # fmt: skip
        r = client.post(
            reverse("api_jobs"), json.dumps(body), content_type="application/json", **bearer(raw)
        )
        assert r.status_code == code, r.content
    assert not Job.objects.exists()


def test_viewer_token_cannot_operate_even_with_operate_scope(client, viewer, online):
    raw = ApiToken.issue(viewer, "x", Scope.OPERATE)[
        1
    ]  # the form never offers it; the API still checks
    body = json.dumps({"action": "deploy_api", "servers": ["web-01"]})
    r = client.post(reverse("api_jobs"), body, content_type="application/json", **bearer(raw))
    assert r.status_code == 403


def test_prometheus_metrics(client, read_token, fake_relay):
    server = make_server("web-02")[0]
    AgentClient(server, fake_relay).heartbeat(report())
    Monitor.objects.create(name='quote"d', url="https://example.com", status=MonitorStatus.UP,
                           last_response_ms=87)  # fmt: skip
    r = client.get(reverse("metrics"), **bearer(read_token))
    assert r.status_code == 200 and r["Content-Type"].startswith("text/plain; version=0.0.4")
    text = r.content.decode()
    assert 'sm_server_up{server="web-02",environment="production"} 1' in text
    assert 'sm_server_cpu_percent{server="web-02",environment="production"} 12.5' in text
    assert (
        'sm_service_up{server="web-02",environment="production",service="nginx",kind="systemd"} 1'
        in text
    )
    assert 'sm_monitor_response_ms{monitor="quote\\"d",kind="http"} 87' in text
    assert "# TYPE sm_alerts_open gauge" in text
    assert client.get(reverse("metrics")).status_code == 401


def test_token_page_shows_the_token_once_and_revokes(client, operator):
    client.force_login(operator)
    r = client.post(reverse("api_tokens"), {"name": "ci", "scope": "operate", "expires": "30"})
    raw = r.context["new_token"]
    assert raw.startswith("smt_") and raw in r.content.decode()
    assert raw not in client.get(reverse("api_tokens")).content.decode()
    token = ApiToken.objects.get()
    assert token.scope == Scope.OPERATE and token.expires_at > timezone.now() + timedelta(days=29)
    client.post(reverse("api_token_revoke", args=[token.pk]))
    assert not ApiToken.objects.get().is_active
    assert {a.action for a in AuditLog.objects.all()} >= {"api_token.create", "api_token.revoke"}


def test_viewers_can_only_create_read_tokens_and_not_revoke_others(client, viewer, operator):
    client.force_login(viewer)
    client.post(reverse("api_tokens"), {"name": "x", "scope": "operate", "expires": "30"})
    assert not ApiToken.objects.exists()  # "operate" isn't a valid choice for a Viewer
    theirs = ApiToken.issue(operator, "theirs", Scope.READ)[0]
    assert client.post(reverse("api_token_revoke", args=[theirs.pk])).status_code == 404
