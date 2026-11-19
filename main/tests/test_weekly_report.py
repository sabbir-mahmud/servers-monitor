"""The weekly summary sent on Sunday evenings."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.alerts import delivery, reports
from apps.alerts.models import Alert, AlertKind, Channel, Delivery
from apps.devices.models import Device, ScreenTime, WebActivity
from apps.monitors.models import Monitor, MonitorCheck

from . import test_devices
from .conftest import acme

pytestmark = pytest.mark.django_db
family, sara = test_devices.family, test_devices.sara

SUNDAY_EVENING = timezone.make_aware(datetime(2026, 11, 15, 18, 5))  # a Sunday


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send", lambda c, m, *_: sent.append((c.name, m.plain)))
    return sent


def channel(account, **kw):
    c = Channel.objects.create(account=account, name=kw.pop("name", "mum"), kind="telegram",
                               telegram_token="t", telegram_chat_id="1",
                               **{"weekly_report": True, **kw})  # fmt: skip
    Channel.objects.filter(pk=c.pk).update(created_at=SUNDAY_EVENING - timedelta(days=30))
    return c


def test_boundary_is_the_last_sunday_evening():
    assert reports.last_boundary(SUNDAY_EVENING) == SUNDAY_EVENING.replace(minute=0)
    before = SUNDAY_EVENING.replace(hour=17)
    assert reports.last_boundary(before) == SUNDAY_EVENING.replace(minute=0) - timedelta(days=7)


def test_family_report_has_screen_time_sites_and_arrivals(family, sara, outbox):
    pc = Device.objects.create(account=family, person=sara, platform="windows", name="Family PC")
    day = (SUNDAY_EVENING - timedelta(days=2)).date()
    ScreenTime.objects.create(device=pc, day=day, os_user="sara", app="Minecraft", seconds=7200)
    ScreenTime.objects.create(device=pc, day=day, os_user="sara", app="Chrome", seconds=3600)
    WebActivity.objects.create(device=pc, day=day, os_user="sara", browser="chrome",
                               domain="youtube.com", visits=4, seconds=1800)  # fmt: skip
    a = Alert.objects.create(account=family, device=pc, kind=AlertKind.PLACE, is_event=True,
                             subject="x:in:1", severity="warning", message="Sara arrived",
                             resolved_at=SUNDAY_EVENING)  # fmt: skip
    Alert.objects.filter(pk=a.pk).update(opened_at=SUNDAY_EVENING - timedelta(days=1))
    channel(family)
    assert reports.process(SUNDAY_EVENING) == 1
    text = outbox[0][1]
    assert "Your week in" in text and "Nov 8 – Nov 15" in text
    assert "Sara: screen time 3 h 00 min" in text and "most on Minecraft, Chrome" in text
    assert "top sites youtube.com" in text and "1 arrival at your places" in text
    assert "Servers" not in text and "Applications" not in text  # not a family's features


def test_sent_once_per_week_and_not_late(family, outbox):
    c = channel(family)
    assert reports.process(SUNDAY_EVENING) == 1
    assert reports.process(SUNDAY_EVENING + timedelta(minutes=30)) == 0
    assert Delivery.objects.get(channel=c).event == "report"
    channel(family, name="late")
    assert reports.process(SUNDAY_EVENING + timedelta(days=2)) == 0  # missed: wait a week


def test_opt_out_and_new_channels_wait(family, outbox):
    channel(family, name="off", weekly_report=False)
    c = channel(family, name="new")
    Channel.objects.filter(pk=c.pk).update(created_at=SUNDAY_EVENING)
    assert reports.process(SUNDAY_EVENING) == 0


def test_company_report_has_app_uptime_and_server_alerts(outbox, server):
    m = Monitor.objects.create(account=acme(), name="Website", url="https://x.example")
    for ok in (True, True, True, False):
        MonitorCheck.objects.create(monitor=m, ok=ok)
    a = Alert.objects.create(account=acme(), server=server, kind=AlertKind.CPU,
                             severity="warning", message="cpu")  # fmt: skip
    Alert.objects.filter(pk=a.pk).update(opened_at=timezone.now() - timedelta(days=1))
    msg = reports.compose(acme(), timezone.now() + timedelta(minutes=1))
    assert "Website: 75.00% up" in msg.plain
    assert "Server alerts: 1 × high cpu" in msg.plain


def test_a_quiet_week(family):
    assert "A quiet week" in reports.compose(family, SUNDAY_EVENING).plain


def test_new_channels_default_to_the_report(client, admin_user):
    from django.urls import reverse

    client.force_login(admin_user)
    page = client.get(reverse("alerts:channel_create") + "?kind=telegram").content.decode()
    tag = (
        page.split('name="weekly_report"')[0].rsplit("<input", 1)[1]
        + page.split('name="weekly_report"')[1].split(">", 1)[0]
    )
    assert "checked" in tag
