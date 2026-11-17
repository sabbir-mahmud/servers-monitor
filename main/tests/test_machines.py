"""Servers and personal devices share one Machine table (multi-table inheritance)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import Device, Machine
from apps.fleet.models import Server

from .conftest import add_member, make_account, make_server


@pytest.fixture
def solo(client, django_user_model):
    me = django_user_model.objects.create_user("dev", "dev@example.com", "pw-x-1234567")
    account = make_account("Me", kind="solo")
    add_member(me, "owner", account)
    client.force_login(me)
    return account


def test_one_table_holds_both_kinds_with_the_same_id(solo):
    server, _ = make_server("web-01", account=solo)
    laptop = Device.objects.create(account=solo, platform="macos", name="Laptop")
    rows = {m.pk: m for m in Machine.objects.filter(account=solo)}
    assert rows[server.pk].kind == "server" and rows[server.pk].details == server
    assert rows[laptop.pk].kind == "device" and rows[laptop.pk].details == laptop


def test_names_are_unique_across_servers_and_devices(solo):
    make_server("box", account=solo)
    clash = Device(account=solo, platform="linux", name="BOX")
    with pytest.raises(ValidationError, match="has that name"):
        clash.validate_constraints()


def test_deleting_a_server_or_device_removes_its_row(solo):
    server, _ = make_server("web-01", account=solo)
    laptop = Device.objects.create(account=solo, platform="macos", name="Laptop")
    server.delete()
    laptop.delete()
    assert not Machine.objects.filter(account=solo).exists()


def test_a_heartbeat_style_update_reaches_both_tables(solo):
    server, _ = make_server("web-01", account=solo)
    now = timezone.now()
    Server.objects.filter(pk=server.pk).update(last_seen_at=now, cpu_percent=12.5)
    server.refresh_from_db()
    assert server.last_seen_at == now and server.cpu_percent == 12.5


def test_solo_page_is_one_table_in_a_fixed_number_of_queries(client, solo):
    def page_queries():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(reverse("fleet:dashboard")).status_code == 200
        return len(ctx)

    make_server("web-01", account=solo)
    Device.objects.create(account=solo, platform="ios", name="Phone", token_hash="t1")
    page_queries()  # the first request also loads the session and user once
    before = page_queries()
    for i in range(5):
        make_server(f"web-1{i}", account=solo)
        Device.objects.create(account=solo, platform="macos", name=f"Mac {i}", token_hash=f"m{i}")
    assert page_queries() == before  # no query per row

    page = client.get(reverse("fleet:dashboard")).content.decode()
    assert page.count('class="srow srow-mixed') == 12  # one row each, one table
    servers_only = client.get(reverse("fleet:dashboard"), {"kind": "server"}).content.decode()
    assert ">web-01</a>" in servers_only and ">Phone</a>" not in servers_only


def test_a_company_sees_only_servers_here(client, admin_user):
    make_server("web-01")
    client.force_login(admin_user)
    acme = Server.objects.get(name="web-01").account
    Device.objects.create(account=acme, platform="macos", name="Reception Mac")
    page = client.get(reverse("fleet:dashboard")).content.decode()
    assert ">web-01</a>" in page and "Reception Mac" not in page
