"""3.1: containers, top processes, temperatures and GPUs in heartbeats."""

import json

import pytest

from apps.agent_api.schema import clean_report
from apps.alerts.models import Alert, AlertKind, Severity

from .conftest import report

pytestmark = pytest.mark.django_db


def _inv(containers=(), temps=(), procs=20):
    return {
        "containers": list(containers),
        "processes": {"cpu": [{"pid": i, "name": f"p{i}", "user": "www", "cpu": 50 - i, "rss": 2**20 * i,
                               "cmd": "x" * 500} for i in range(procs)],
                      "memory": [], "count": procs},
        "temps": list(temps),
        "gpus": [{"name": "A100", "util": 40, "mem_used": 2**30, "mem_total": 2**34, "temp": 61}],
    }  # fmt: skip


def test_schema_bounds_everything():
    body = report()
    body["inventory"] = _inv(
        containers=[{"id": "a" * 64, "name": "n" * 300, "state": "running"}] * 80, procs=40
    )
    body["capabilities"]["features"] = ["job_ping", "inventory", 7]
    clean = clean_report(body)
    inv = clean["inventory"]
    assert len(inv["containers"]) == 50 and len(inv["containers"][0]["id"]) == 12
    assert len(inv["containers"][0]["name"]) == 80
    assert len(inv["processes"]["cpu"]) == 10 and len(inv["processes"]["cpu"][0]["cmd"]) == 120
    assert clean["capabilities"]["features"] == ["job_ping", "inventory"]
    assert len(json.dumps(clean)) < 32 * 1024  # the heartbeat budget


def test_old_agents_send_no_inventory():
    assert clean_report(report())["inventory"] == {}


def _beat(agent, **inv):
    body = report()
    body["inventory"] = _inv(**inv)
    agent.heartbeat(body)


def test_container_that_stops_alerts_and_recovers(agent):
    web = {"id": "1" * 12, "name": "web", "image": "nginx", "state": "running"}
    old = {"id": "2" * 12, "name": "old-job", "image": "x", "state": "exited"}  # stopped all along
    _beat(agent, containers=[web, old])
    assert not Alert.objects.filter(kind=AlertKind.CONTAINER).exists()
    _beat(agent, containers=[web | {"state": "exited"}, old])
    alert = Alert.objects.get(kind=AlertKind.CONTAINER, resolved_at=None)
    assert alert.subject == "web" and alert.severity == Severity.CRITICAL
    _beat(agent, containers=[web | {"state": "exited"}, old])  # still stopped: still one alert
    assert Alert.objects.filter(kind=AlertKind.CONTAINER).count() == 1
    _beat(agent, containers=[web, old])
    alert.refresh_from_db()
    assert alert.resolved_at is not None


def test_temperature_alerts_use_the_sensors_marks(agent):
    cpu = {"sensor": "coretemp Package id 0", "high": 80, "critical": 100}
    _beat(agent, temps=[cpu | {"current": 85}])
    alert = Alert.objects.get(kind=AlertKind.TEMPERATURE, resolved_at=None)
    assert alert.severity == Severity.WARNING
    _beat(agent, temps=[cpu | {"current": 101}])
    alert.refresh_from_db()
    assert alert.severity == Severity.CRITICAL
    _beat(agent, temps=[cpu | {"current": 79}])  # within hysteresis: stays open
    assert Alert.objects.filter(kind=AlertKind.TEMPERATURE, resolved_at=None).exists()
    _beat(agent, temps=[cpu | {"current": 60}])
    assert not Alert.objects.filter(kind=AlertKind.TEMPERATURE, resolved_at=None).exists()


def test_server_page_shows_inventory(client, viewer, agent):
    _beat(agent, containers=[{"id": "1" * 12, "name": "web", "image": "nginx:1.27", "state": "running",
                              "cpu": 3.5, "memory": 50 * 2**20}],
          temps=[{"sensor": "nvme Composite", "current": 41, "high": 80}])  # fmt: skip
    client.force_login(viewer)
    from django.urls import reverse

    page = client.get(reverse("fleet:server_detail", args=[agent.server.pk])).content.decode()
    assert (
        "Top processes" in page
        and "nginx:1.27" in page
        and "nvme Composite" in page
        and "A100" in page
    )


def test_heartbeat_with_inventory_stays_in_query_budget(agent, django_assert_max_num_queries):
    _beat(agent, containers=[{"id": "1" * 12, "name": "web", "state": "running"}])
    with django_assert_max_num_queries(12):
        _beat(agent, containers=[{"id": "1" * 12, "name": "web", "state": "running"}],
              temps=[{"sensor": "cpu", "current": 50, "high": 80}])  # fmt: skip
