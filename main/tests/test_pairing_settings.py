"""Account settings > Pairing: computers pair through the NexGuard app unless the account
also allows the one-line command (Account.computer_pairing)."""

import json

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.tenants.models import Account

from . import test_devices
from .conftest import add_member
from .test_devices import add_device, api_pair

family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

pytestmark = pytest.mark.django_db

APP = {"client": "desktop-app"}


def pair_without_app(http, code, platform, **extra):
    """What a terminal install sends: no `client` (api_pair adds the app's for computers)."""
    body = {"code": code, "platform": platform, **extra}
    return http.post(reverse("device_api_pair"), json.dumps(body), content_type="application/json")


def test_new_accounts_need_the_app_for_computers(db):
    account = Account.objects.create(name="New", kind="family")
    assert account.computer_pairing == "app" and account.computers_need_app


def test_a_computer_pairing_without_the_app_is_refused_with_how_to(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="windows", name="Family PC")
    for preview in (True, False):  # the preview refuses too, before anyone is asked
        resp = pair_without_app(client, code, "windows", preview=preview)
        assert resp.status_code == 403
        assert "connects computers with the NexGuard app" in resp.json()["error"]
    device.refresh_from_db()
    assert device.pairing_code_hash and not device.is_paired  # the code still works
    assert api_pair(client, code, platform="windows").status_code == 201


def test_the_one_line_command_works_when_the_account_allows_it(client, parent, sara, family):
    family.computer_pairing = "app_or_command"
    family.save()
    client.force_login(parent)
    _, code = add_device(client, sara, platform="linux", name="Laptop")
    assert (
        pair_without_app(
            client,
            code,
            "linux",
        ).status_code
        == 201
    )


def test_phones_are_never_affected(client, parent, sara):
    client.force_login(parent)
    _, code = add_device(client, sara, platform="android")
    assert api_pair(client, code, platform="android").status_code == 201  # no client sent


def test_owner_changes_pairing_and_it_is_audited(client, parent, family):
    client.force_login(parent)
    page = client.get(reverse("tenants:settings")).content.decode()
    assert 'id="pairing"' in page and "With the NexGuard app" in page and "Recommended" in page
    resp = client.post(reverse("tenants:settings"),
                       {"action": "pairing", "computer_pairing": "app_or_command"})  # fmt: skip
    assert resp.status_code == 302 and resp["Location"].endswith("#pairing")
    family.refresh_from_db()
    assert family.computer_pairing == "app_or_command"
    assert AuditLog.objects.filter(action="account.pairing", target="The Rahmans").exists()
    bad = client.post(reverse("tenants:settings"), {"action": "pairing", "computer_pairing": "x"})
    assert bad.status_code == 200 and "field-error" in bad.content.decode()


def test_members_who_cant_change_the_account_only_see_the_setting(
    client, django_user_model, family
):
    guardian = django_user_model.objects.create_user("dad", "dad@example.com", "pw-x-1234567")
    add_member(guardian, "guardian", family)
    client.force_login(guardian)
    page = client.get(reverse("tenants:settings")).content.decode()
    assert 'id="pairing"' in page and "Only an owner can change how computers connect" in page
    client.post(
        reverse("tenants:settings"), {"action": "pairing", "computer_pairing": "app_or_command"}
    )
    family.refresh_from_db()
    assert family.computer_pairing == "app"
