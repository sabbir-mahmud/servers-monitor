"""Children's (and staff) devices: adding, pairing with a code, checking in."""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.devices import services
from apps.devices.models import Device, DeviceLocation
from apps.tenants.models import Person

from .conftest import add_member, make_account

pytestmark = pytest.mark.django_db


@pytest.fixture
def family():
    return make_account("The Rahmans", kind="family")


@pytest.fixture
def parent(django_user_model, family):
    user = django_user_model.objects.create_user("mum", "mum@example.com", "pw-x-1234567")
    add_member(user, "owner", family)
    return user


@pytest.fixture
def sara(family):
    return Person.objects.create(account=family, kind="child", name="Sara")


def add_device(client, person, platform="android", name="Sara's phone", with_page=False):
    """As the parent: add the device and read the code off the pairing page."""
    resp = client.post(
        reverse("devices:create"), {"platform": platform, "name": name, "person": person.pk}
    )
    device = Device.objects.get(name=name)
    assert resp["Location"] == reverse("devices:pair", args=[device.pk])
    page = client.get(resp["Location"]).content.decode()
    code = page.split('id="pair-code">')[1].split("<")[0]
    return (device, code, page) if with_page else (device, code)


def api_pair(client, code, platform="android", **extra):
    body = {"code": code, "platform": platform, "model": "Pixel 9", "os_version": "Android 16"}
    return client.post(reverse("device_api_pair"), json.dumps({**body, **extra}),
                       content_type="application/json")  # fmt: skip


def api_checkin(client, token, body):
    return client.post(reverse("device_api_checkin"), json.dumps(body),
                       content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")  # fmt: skip


# --------------------------------------------------------------------------- adding


def test_parent_adds_a_phone_and_sees_the_code_once(client, parent, sara):
    client.force_login(parent)
    page = client.get(reverse("tenants:person_detail", args=[sara.pk])).content.decode()
    for label in ("Windows", "Mac", "Linux", "Android", "iPhone or iPad"):
        assert label in page
    device, code, first = add_device(client, sara, with_page=True)
    assert len(code) == 9 and code[4] == "-"
    assert "<svg" in first and "Scan the QR code" in first  # for the phone app
    assert device.account == sara.account and device.person == sara and not device.is_paired
    assert code.replace("-", "") not in device.pairing_code_hash  # stored hashed
    again = client.get(reverse("devices:pair", args=[device.pk])).content.decode()
    assert code not in again and "already shown" in again
    assert AuditLog.objects.filter(action="device.create", target="Sara's phone").exists()


def test_child_card_lists_devices_with_their_state(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    page = client.get(reverse("tenants:people")).content.decode()
    assert "Sara&#x27;s phone" in page and "Waiting to pair" in page
    api_pair(client, code)
    page = client.get(reverse("tenants:people")).content.decode()
    assert "All devices online" in page


def test_device_names_are_unique_per_account(client, parent, sara):
    client.force_login(parent)
    add_device(client, sara)
    resp = client.post(reverse("devices:create"),
                       {"platform": "ios", "name": "sara's PHONE", "person": sara.pk})  # fmt: skip
    assert "has that name" in resp.content.decode()


# --------------------------------------------------------------------------- pairing


def test_pairing_gives_a_token_and_uses_up_the_code(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    client.logout()  # the device has no session
    resp = api_pair(client, code.lower().replace("-", " "))  # however it was typed
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"].startswith("ngd_") and body["device"]["person"] == "Sara"
    assert body["checkin_seconds"] == 900 and "The Rahmans will see" in body["disclosure"]
    device.refresh_from_db()
    assert device.is_paired and device.model == "Pixel 9" and device.pairing_code_hash == ""
    assert api_pair(client, code).status_code == 404  # single use
    assert AuditLog.objects.filter(action="device.pair", account=device.account).exists()


def test_the_code_is_for_one_kind_of_device(client, parent, sara):
    client.force_login(parent)
    _, code = add_device(client, sara, platform="ios", name="iPad")
    resp = api_pair(client, code, platform="windows")
    assert resp.status_code == 409 and "iPhone or iPad" in resp.json()["error"]
    assert api_pair(client, code, platform="ios").status_code == 201  # not used up


def test_codes_do_not_expire(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    Device.objects.filter(pk=device.pk).update(created_at=timezone.now() - timedelta(days=400))
    assert api_pair(client, code).status_code == 201


def test_bad_codes_and_guessing(client, settings):
    assert api_pair(client, "short").status_code == 400
    assert api_pair(client, "AAAA-BBBB").status_code == 404
    for _ in range(10):
        api_pair(client, "AAAA-BBBB")
    assert api_pair(client, "AAAA-BBBB").status_code == 429


def test_a_reset_device_is_paired_again_and_the_old_token_dies(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    old = api_pair(client, code).json()["token"]
    assert api_checkin(client, old, {}).status_code == 200
    # Phone was reset: parent makes a new code. The old pairing keeps working meanwhile.
    resp = client.post(reverse("devices:pair", args=[device.pk]), {"action": "new_code"})
    page = client.get(resp["Location"]).content.decode()
    assert "Pair Sara&#x27;s phone again" in page
    new_code = page.split('id="pair-code">')[1].split("<")[0]
    assert api_checkin(client, old, {}).status_code == 200
    new = api_pair(client, new_code).json()["token"]
    assert api_checkin(client, old, {}).status_code == 401
    assert api_checkin(client, new, {}).status_code == 200
    assert Device.objects.count() == 1  # same entry, history kept


def test_a_new_code_cancels_the_previous_one(client, parent, sara):
    client.force_login(parent)
    device, first = add_device(client, sara)
    resp = client.post(reverse("devices:pair", args=[device.pk]), {"action": "new_code"})
    client.get(resp["Location"])
    assert api_pair(client, first).status_code == 404


# --------------------------------------------------------------------------- check-ins


@pytest.fixture
def paired(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    token = api_pair(client, code).json()["token"]
    return device, token


def test_checkin_records_battery_storage_network_and_location(client, paired):
    device, token = paired
    resp = api_checkin(client, token, {
        "battery": {"percent": 42, "charging": False},
        "storage": {"free": 20 * 2**30, "total": 128 * 2**30},
        "network": {"type": "wifi", "name": "Home"},
        "location": {"lat": 23.810331, "lon": 90.412521, "accuracy": 12, "source": "gps"},
        "os_version": "Android 16.1",
    })  # fmt: skip
    assert resp.status_code == 200 and resp.json()["checkin_seconds"] == 900
    device.refresh_from_db()
    assert (device.battery_percent, device.charging, device.network) == (42, False, "wifi")
    assert device.storage_used_percent == 84 and device.os_version == "Android 16.1"
    assert float(device.latitude) == pytest.approx(23.810331) and device.location_accuracy_m == 12
    assert DeviceLocation.objects.filter(device=device).count() == 1
    assert device.state == "online"
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "42%" in page and "23.81033, 90.41252" in page and "openstreetmap.org" in page
    assert "within 12 m" in page and "Home" in page


def test_nonsense_is_ignored_not_fatal(client, paired):
    device, token = paired
    resp = api_checkin(client, token, {
        "battery": {"percent": 250, "charging": "yes"},
        "storage": {"free": 10, "total": 5},
        "network": {"type": "carrier-pigeon"},
        "location": {"lat": 123, "lon": 10},
        "model": {"not": "a string"},
    })  # fmt: skip
    assert resp.status_code == 200
    device.refresh_from_db()
    assert device.battery_percent is None and device.charging is None
    assert device.storage_total is None and device.network == "" and device.latitude is None
    assert device.model == "" and not DeviceLocation.objects.exists()
    bad = client.post(reverse("device_api_checkin"), "[1,2]", content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {token}")  # fmt: skip
    assert bad.status_code == 400


def test_old_or_future_fixes_are_clamped_to_now(client, paired):
    device, token = paired
    future = (timezone.now() + timedelta(days=1)).isoformat()
    api_checkin(client, token, {"location": {"lat": 1, "lon": 2, "at": future}})
    loc = DeviceLocation.objects.get()
    assert abs((loc.recorded_at - timezone.now()).total_seconds()) < 60


def test_removed_device_is_told_to_pair_again(client, paired, parent):
    device, token = paired
    client.force_login(parent)
    api_checkin(client, token, {"location": {"lat": 1, "lon": 2}})
    client.post(reverse("devices:delete", args=[device.pk]))
    assert not Device.objects.exists() and not DeviceLocation.objects.exists()
    resp = api_checkin(client, token, {})
    assert resp.status_code == 401 and "Pair it again" in resp.json()["error"]


def test_no_token_or_a_server_token_is_refused(client, paired):
    assert client.post(reverse("device_api_checkin"), "{}",
                       content_type="application/json").status_code == 401  # fmt: skip
    assert api_checkin(client, "smk_not-a-device-token", {}).status_code == 401


def test_goes_offline_after_missed_checkins(paired):
    device, _ = paired
    Device.objects.filter(pk=device.pk).update(last_seen_at=timezone.now() - timedelta(minutes=40))
    device.refresh_from_db()
    assert device.state == "offline"
    laptop = Device(platform="windows", token_hash="x", last_seen_at=timezone.now()
                    - timedelta(minutes=13))  # fmt: skip
    assert laptop.checkin_seconds == 300 and laptop.state == "offline"


def test_location_history_is_pruned(paired, settings):
    device, _ = paired
    settings.DEVICE_LOCATION_DAYS = 30
    now = timezone.now()
    for days in (1, 31, 60):
        DeviceLocation.objects.create(device=device, recorded_at=now - timedelta(days=days),
                                      latitude=1, longitude=2)  # fmt: skip
    assert services.prune_locations() == 2 and DeviceLocation.objects.count() == 1


# --------------------------------------------------------------------------- who may do what


def test_family_viewer_sees_but_cannot_add_or_remove(client, django_user_model, family, paired):
    device, _ = paired
    grandma = django_user_model.objects.create_user("gran", "g@example.com", "pw-x-1234567")
    add_member(grandma, "viewer", family)
    client.force_login(grandma)
    assert client.get(reverse("devices:detail", args=[device.pk])).status_code == 200
    for name in ("devices:pair", "devices:update", "devices:delete"):
        assert client.get(reverse(name, args=[device.pk])).status_code == 403
    assert client.get(reverse("devices:create")).status_code == 403
    assert "Add device" not in client.get(reverse("tenants:people")).content.decode()


def test_guardian_manages_devices(client, django_user_model, family, sara):
    dad = django_user_model.objects.create_user("dad", "d@example.com", "pw-x-1234567")
    add_member(dad, "guardian", family)
    client.force_login(dad)
    add_device(client, sara, platform="macos", name="Sara's MacBook")


@pytest.fixture
def solo_dev(client, django_user_model):
    me = django_user_model.objects.create_user("dev", "dev@example.com", "pw-x-1234567")
    add_member(me, "owner", make_account("Me", kind="solo"))
    client.force_login(me)
    return me


def test_solo_devices_page_offers_a_server_or_a_computer_or_phone(client, solo_dev):
    page = client.get(reverse("fleet:dashboard")).content.decode()
    assert "No servers or devices connected yet." in page
    assert reverse("fleet:server_create") in page and reverse("devices:create") in page


def test_solo_developer_connects_their_own_laptop(client, solo_dev):
    form = client.get(reverse("devices:create")).content.decode()
    assert "Whose is it?" not in form and "Connect a computer or phone" in form
    resp = client.post(reverse("devices:create"), {"platform": "macos", "name": "My Mac"})
    device = Device.objects.get(name="My Mac")
    assert device.person is None and device.account.kind == "solo"
    code = client.get(resp["Location"]).content.decode().split('id="pair-code">')[1].split("<")[0]
    token = api_pair(client, code, platform="macos").json()["token"]
    api_checkin(client, token, {"battery": {"percent": 80}})

    page = client.get(reverse("fleet:dashboard")).content.decode()
    assert ">My Mac</a>" in page and "80%" in page
    detail = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert f'href="{reverse("fleet:dashboard")}"' in detail and "Belongs to" not in detail
    resp = client.post(reverse("devices:delete", args=[device.pk]))
    assert resp["Location"] == reverse("fleet:dashboard")


def test_another_familys_device_is_invisible(client, django_user_model, paired):
    device, _ = paired
    other = django_user_model.objects.create_user("other", "o@example.com", "pw-x-1234567")
    add_member(other, "owner", make_account("Neighbours", kind="family"))
    client.force_login(other)
    for name in ("devices:detail", "devices:pair", "devices:update", "devices:delete"):
        assert client.get(reverse(name, args=[device.pk])).status_code == 404
    assert client.post(reverse("devices:pair", args=[device.pk])).status_code == 404
    assert "Sara" not in client.get(reverse("tenants:people")).content.decode()
    # Can't assign a device to someone else's child either.
    resp = client.post(reverse("devices:create"),
                       {"platform": "android", "name": "x", "person": device.person_id})  # fmt: skip
    assert resp.status_code == 200 and not Device.objects.filter(name="x").exists()


# --------------------------------------------------------------------------- the parents' page


def test_people_page_sums_up_each_child(client, parent, family, sara):
    now = timezone.now()
    Device.objects.create(account=family, person=sara, platform="ios", name="Sara's iPhone",
                          token_hash="t1", last_seen_at=now, battery_percent=9, charging=False,
                          latitude=23.78, longitude=90.40, location_at=now)  # fmt: skip
    Device.objects.create(account=family, person=sara, platform="windows", name="Family PC",
                          token_hash="t2", last_seen_at=now - timedelta(hours=5))  # fmt: skip
    Person.objects.create(account=family, kind="child", name="Zayn")
    client.force_login(parent)
    page = client.get(reverse("tenants:people")).content.decode()
    assert "2 devices need a look" in page and 'class="prow is-critical"' in page
    assert 'class="batt is-low"' in page and "openstreetmap.org/?mlat=23.780000" in page
    assert '1<span class="statbar-of"> / 2</span>' in page
    # A child with nothing yet gets a one-click "Add device" for them.
    assert f"?person={Person.objects.get(name='Zayn').pk}" in page


def test_parents_are_not_told_about_the_server_relay(client, parent, viewer):
    """The relay only matters for servers; a parent has none."""
    client.force_login(parent)
    assert "relay process" not in client.get(reverse("tenants:people")).content.decode()
    client.force_login(viewer)  # a company member who can see servers still is
    assert "relay process" in client.get(reverse("fleet:dashboard")).content.decode()


# --------------------------------------------------------------------------- maps and the redesigned pages


def test_static_map_centres_the_pin_and_draws_the_trail(settings):
    from apps.devices import maps

    settings.MAP_TILE_URL = "https://tiles.test/{z}/{x}/{y}.png"
    m = maps.static_map(
        23.7812, 90.4074, accuracy_m=36, trail=[(23.7806, 90.4071), (23.7812, 90.4074)]
    )
    assert m["zoom"] == 16 and len(m["tiles"]) == 15
    assert all(t["url"].startswith("https://tiles.test/16/") for t in m["tiles"])
    # The pin sits inside the middle tile of the 5 × 3 grid.
    assert 512 <= m["pin_x"] < 768 and 256 <= m["pin_y"] < 512
    assert m["trail"].count(",") == 2 and m["radius"] is not None
    assert maps.zoom_for(5000) == 11  # a vague fix zooms out so the circle fits
    settings.MAP_TILE_URL = ""
    assert maps.static_map(1, 2) is None


def test_tile_server_is_allowed_by_the_csp(client, parent):
    client.force_login(parent)
    csp = client.get(reverse("tenants:people"))["Content-Security-Policy"]
    assert "https://tile.openstreetmap.org" in csp


def test_child_page_shows_map_recent_places_and_devices(client, parent, paired, sara):
    device, token = paired
    api_checkin(client, token, {"location": {"lat": 23.7812, "lon": 90.4074, "accuracy": 20},
                                "battery": {"percent": 9, "charging": False}})  # fmt: skip
    page = client.get(reverse("tenants:person_detail", args=[sara.pk])).content.decode()
    assert "Where Sara was last" in page and 'class="map-tile"' in page
    assert "23.7812, 90.4074" in page  # recent places
    assert "Sara&#x27;s phone: battery low" in page  # status line
    assert "Add another" not in page and "Full history" in page  # adding is in the header menu
    assert 'class="alert-line is-warning"' in page  # low battery is called out up top


def test_device_page_shows_timeline_and_vitals(client, paired):
    device, token = paired
    for lat in (23.780, 23.781):  # ~110 m apart: both on the street-level map
        api_checkin(
            client, token, {"location": {"lat": lat, "lon": 90.4}, "battery": {"percent": 9}}
        )
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "Today" in page and page.count('class="timeline-item"') == 2
    assert 'class="map-trail"' in page and "Battery at 9%" in page
    assert 'aria-valuenow="9"' in page


def test_maps_can_be_turned_off(client, paired, settings):
    device, token = paired
    settings.MAP_TILE_URL = ""
    api_checkin(client, token, {"location": {"lat": 23.78, "lon": 90.4}})
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "map-tile" not in page and "23.780000, 90.400000" in page and "Open map" in page


def test_computers_without_location_skip_the_history_card(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="windows", name="Family PC")
    token = api_pair(client, code, platform="windows").json()["token"]
    api_checkin(client, token, {})
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    # No location section at all for a computer: nothing to show and nothing to do.
    assert "Location history" not in page and "No location yet" not in page
    assert "About this device" in page and "Remove device" in page


# --------------------------------------------------------------------------- the people list


def test_people_list_searches_and_puts_problems_first(client, parent, family, paired):
    device, token = paired
    api_checkin(client, token, {"battery": {"percent": 5, "charging": False}})  # Sara: low
    Person.objects.create(account=family, kind="child", name="Abir")
    Person.objects.create(account=family, kind="child", name="Zayn")
    url = reverse("tenants:people")

    page = client.get(url).content.decode()
    # Sara needs a look, so she leads, ahead of alphabetical order.
    assert page.index(">Sara</a>") < page.index(">Abir</a>") < page.index(">Zayn</a>")
    assert 'title="1 need a look"' in page  # the red count on the sidebar link
    assert "plist-filter" not in page  # no filter tabs, just search

    page = client.get(url, {"q": "zay"}).content.decode()
    assert ">Zayn</a>" in page and ">Abir</a>" not in page and "1 of 3" in page
    page = client.get(url, {"q": "nobody"}).content.decode()
    assert "Nobody matches" in page


def test_child_with_only_a_computer_gets_a_short_location_hint(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    token = api_pair(client, code, platform="macos").json()["token"]
    api_checkin(client, token, {"battery": {"percent": 70}})
    page = client.get(reverse("tenants:person_detail", args=[sara.pk])).content.decode()
    assert "map-tile" not in page and "Where Sara was last" not in page
    assert "Computers don&#x27;t share it" in page or "Computers don't share it" in page
    assert f"?person={sara.pk}&amp;platform=ios" in page  # offer to add a phone
    assert "Not on computers" in page and "alert-line" not in page


def test_a_family_can_add_a_partner_but_not_staff(client, parent, family):
    client.force_login(parent)
    page = client.get(reverse("tenants:person_create")).content.decode()
    assert "Spouse or partner" in page and "Staff member" not in page
    client.post(reverse("tenants:person_create"), {"kind": "partner", "name": "Nadia"})
    assert Person.objects.get(name="Nadia").kind == "partner"
    resp = client.post(reverse("tenants:person_create"), {"kind": "staff", "name": "Bob"})
    assert resp.status_code == 200 and not Person.objects.filter(name="Bob").exists()
    page = client.get(reverse("tenants:people")).content.decode()
    assert '<h1 class="page-title">People</h1>' in page and "Spouse or partner" in page


def test_add_device_form_has_no_blank_platform_and_offers_people_as_chips(client, parent, sara):
    client.force_login(parent)
    page = client.get(reverse("devices:create"), {"person": sara.pk}).content.decode()
    assert "Select an option" not in page and "---------" not in page
    assert 'class="person-chip"' in page and ">Sara</span>" in page and ">Shared</span>" in page
    assert "What happens next" in page


def test_adding_or_editing_a_person_lands_on_their_page(client, parent):
    client.force_login(parent)
    resp = client.post(reverse("tenants:person_create"), {"kind": "child", "name": "Mira"})
    mira = Person.objects.get(name="Mira")
    assert resp["Location"] == reverse("tenants:person_detail", args=[mira.pk])
    resp = client.post(reverse("tenants:person_update", args=[mira.pk]),
                       {"kind": "child", "name": "Mira K"})  # fmt: skip
    assert resp["Location"] == reverse("tenants:person_detail", args=[mira.pk])
