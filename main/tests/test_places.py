"""Places: arriving, leaving, missed arrivals, and the pages."""

from datetime import time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts import delivery
from apps.alerts.models import Alert, AlertKind, Channel
from apps.devices import places
from apps.devices.forms import parse_location
from apps.devices.models import DevicePlace, Place

from . import test_devices
from .phones import pair_phone
from .test_devices import add_device

pytestmark = pytest.mark.django_db
family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

SCHOOL = (23.810000, 90.410000)
NEARBY = (23.810500, 90.410300)  # ~60 m away
EDGE = (23.811600, 90.410000)  # ~178 m: beyond 150, inside the leave margin
FAR = (23.830000, 90.410000)  # ~2.2 km


@pytest.fixture
def phone(client, parent, sara):
    """A phone paired through the relay; checks in as the app does (tests/phones.py)."""
    client.force_login(parent)
    device, code = add_device(client, sara)
    app = pair_phone(device, code)
    return app.device, app


@pytest.fixture
def school(family, sara):
    p = Place.objects.create(account=family, name="School", latitude=SCHOOL[0],
                             longitude=SCHOOL[1], radius_m=150)  # fmt: skip
    return p


def at(app, where, accuracy=15):
    app.checkin({"location": {"lat": where[0], "lon": where[1], "accuracy": accuracy}})


def events(device):
    return list(Alert.objects.filter(device=device, is_event=True).order_by("opened_at")
                .values_list("message", flat=True))  # fmt: skip


def test_first_fix_sets_state_quietly_then_arrive_and_leave(client, phone, school):
    device, app = phone
    at(app, NEARBY)
    assert events(device) == []  # already there when the place was drawn
    assert DevicePlace.objects.get(device=device, place=school).inside
    at(app, FAR)
    at(app, NEARBY)
    assert events(device) == ["Sara left School", "Sara arrived at School"]


def test_edge_wobble_and_coarse_fixes_are_ignored(client, phone, school):
    device, app = phone
    at(app, NEARBY)
    at(app, EDGE)
    at(app, FAR, accuracy=5000)
    assert events(device) == [] and DevicePlace.objects.get(device=device).inside


def test_place_for_other_people_ignores_this_device(client, phone, school, family):
    from apps.tenants.models import Person

    device, app = phone
    school.people.add(Person.objects.create(account=family, kind="child", name="Rafi"))
    at(app, NEARBY)
    at(app, FAR)
    assert not DevicePlace.objects.exists() and events(device) == []


def test_events_are_announced_once_and_never_resolved_or_reminded(
    client, phone, school, family, monkeypatch
):
    device, app = phone
    sent = []
    monkeypatch.setattr(delivery, "send", lambda c, m, *_: sent.append(m.plain))
    c = Channel.objects.create(account=family, name="mum", kind="telegram", telegram_token="t",
                               telegram_chat_id="1", repeat_minutes=1)  # fmt: skip
    Channel.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(hours=1))
    at(app, NEARBY)
    at(app, FAR)
    assert delivery.process() == 1
    assert sent[0].startswith("📍 Sara left School")
    assert delivery.process(now=timezone.now() + timedelta(minutes=5)) == 0


def test_missed_arrival_opens_then_resolves_on_arrival(client, phone, school, sara):
    device, app = phone
    school.people.add(sara)
    now = timezone.localtime()
    school.arrive_by = time(0, 0)
    school.arrive_days = [now.weekday()]
    school.save()
    at(app, FAR)  # far away all day
    assert places.sweep() == 1
    assert places.sweep() == 0  # once a day
    alert = Alert.objects.get(kind=AlertKind.PLACE, is_event=False)
    assert alert.message == "Sara hasn't arrived at School (expected by 00:00)"
    assert alert.resolved_at is None
    at(app, NEARBY)
    alert.refresh_from_db()
    assert alert.resolved_at is not None
    assert events(device) == ["Sara arrived at School"]


def test_no_missed_arrival_before_time_or_on_other_days(client, phone, school, sara):
    device, app = phone
    school.people.add(sara)
    school.arrive_by = time(23, 59, 59)
    school.arrive_days = list(range(7))
    school.save()
    assert places.sweep(now=timezone.localtime().replace(hour=8, minute=0)) == 0
    school.arrive_by = time(0, 0)
    school.arrive_days = [(timezone.localtime().weekday() + 1) % 7]
    school.save()
    assert places.sweep() == 0


def test_yesterdays_missed_arrival_closes_at_midnight(client, phone, school, sara):
    device, _ = phone
    old = Alert.objects.create(account=device.account, device=device, kind=AlertKind.PLACE,
                               subject=places.late_subject(school, sara), severity="warning",
                               message="late")  # fmt: skip
    Alert.objects.filter(pk=old.pk).update(opened_at=timezone.now() - timedelta(days=1, hours=1))
    places.sweep()
    old.refresh_from_db()
    assert old.resolved_at is not None


@pytest.mark.parametrize("text,expected", [
    ("23.8103, 90.4125", (23.8103, 90.4125)),
    ("https://www.google.com/maps/@23.8103,90.4125,15z", (23.8103, 90.4125)),
    ("https://www.openstreetmap.org/#map=16/23.8103/90.4125", (23.8103, 90.4125)),
    ("https://maps.google.com/?q=23.8103,90.4125", (23.8103, 90.4125)),
    ("somewhere nice", None),
    ("95, 10", None),
])  # fmt: skip
def test_parse_location(text, expected):
    assert parse_location(text) == expected


def test_parent_adds_edits_and_deletes_a_place(client, parent, sara, phone):
    resp = client.post(reverse("devices:place_create"), {
        "name": "School", "location": "https://www.google.com/maps/@23.81,90.41,15z",
        "radius_m": 200, "notify_arrive": "on", "people": [sara.pk], "arrive_by": "08:30",
        "arrive_days": [0, 1, 2, 3, 4],
    })  # fmt: skip
    assert resp.status_code == 302, resp.content.decode()[:2000]
    place = Place.objects.get()
    assert (float(place.latitude), place.radius_m, place.notify_leave) == (23.81, 200, False)
    page = client.get(reverse("devices:places")).content.decode()
    assert "School" in page and "by 08:30, weekdays" in page
    resp = client.post(reverse("devices:place_update", args=[place.pk]), {
        "name": "School", "location": "", "radius_m": 200, "arrive_by": "08:30",
    })  # fmt: skip
    assert "Tick who" in resp.content.decode()
    client.post(reverse("devices:place_delete", args=[place.pk]))
    assert not Place.objects.exists()


def test_place_form_needs_a_location(client, parent):
    client.force_login(parent)
    resp = client.post(reverse("devices:place_create"), {"name": "Home", "radius_m": 150})
    assert "Type coordinates" in resp.content.decode()


def test_places_are_per_account(client, school, admin_user):
    client.force_login(admin_user)
    assert client.get(reverse("devices:place_update", args=[school.pk])).status_code == 404


def test_distance():
    assert 50 < places.distance_m(*SCHOOL, *NEARBY) < 70
