"""Screen time, websites and blocking on children's and staff computers (Go agent only)."""

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.agent_api import relay
from apps.audit.models import AuditLog
from apps.devices import activity, blocklist
from apps.devices.models import (
    ActivityBatch,
    Device,
    DeviceControls,
    ScreenTime,
    WebActivity,
)
from apps.tenants.models import Person

from .computers import ask, pair_computer
from .conftest import add_member, make_account
from .test_devices import add_device, code_on

pytestmark = pytest.mark.django_db

AGENT = {"app_version": "nexguard-agent 0.1.0"}


@pytest.fixture(autouse=True)
def _blocklist_dir(settings, tmp_path):
    """Blocklist files next to a throwaway database, and never fetched over the network."""
    settings.SQLITE_PATH = tmp_path / "db.sqlite3"
    settings.ADULT_BLOCKLIST_URL = ""


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


def paired_mac(client, person, name="Sara's Mac", info=AGENT):
    device, code = add_device(client, person, platform="macos", name=name)
    pc = pair_computer(device, code, **info)
    device.refresh_from_db()
    return device, pc


@pytest.fixture
def mac(client, parent, sara):
    client.force_login(parent)
    return paired_mac(client, sara)


def turn_on(device, **fields):
    defaults = {"screen_time": True, "web_activity": True}
    return DeviceControls.objects.update_or_create(device=device, defaults={**defaults, **fields})[
        0
    ]


def batch(screen=(), web=(), batch_id=None):
    return {"batch": str(batch_id or uuid.uuid4()), "screen": list(screen), "web": list(web)}


def today():
    return timezone.localdate().isoformat()


# ---------------------------------------------------------------- who can have controls


def test_only_children_and_staff_on_the_agent_get_controls(client, parent, family, sara):
    client.force_login(parent)
    child_mac, pc = paired_mac(client, sara)
    assert child_mac.controls_allowed
    assert pc.send(AGENT)["controls"] == {
        "screen_time": False, "web_activity": False, "location": False, "blocking": ""}  # fmt: skip

    partner = Person.objects.create(account=family, kind="partner", name="Rahim")
    their_mac, pc = paired_mac(client, partner, name="Rahim's Mac")
    assert "child's or a staff member's" in their_mac.controls_block_reason
    assert pc.send(AGENT)["controls"] is None

    script_mac, pc = paired_mac(client, sara, name="Sara's old Mac",
                                info={"app_version": "nexguard-device-sh 1.2.0"})  # fmt: skip
    assert "agent" in script_mac.controls_block_reason
    assert pc.send({"app_version": "nexguard-device-sh 1.2.0"})["controls"] is None


def test_staff_computers_in_a_company_can_have_controls(client, django_user_model):
    company = make_account("Acme", kind="company")
    boss = django_user_model.objects.create_user("boss", "boss@example.com", "pw-x-1234567")
    add_member(boss, "owner", company)
    staff = Person.objects.create(account=company, kind="staff", name="Karim")
    client.force_login(boss)
    device, _ = paired_mac(client, staff, name="Karim's Mac")
    assert device.controls_allowed


def test_a_solo_developers_own_computer_can_have_controls(client, django_user_model):
    solo = make_account("Sabbir", kind="solo")
    me = django_user_model.objects.create_user("me", "me@example.com", "pw-x-1234567")
    add_member(me, "owner", solo)
    client.force_login(me)
    resp = client.post(reverse("devices:create"), {"platform": "macos", "name": "My Mac"})
    device = Device.objects.get(name="My Mac")
    code = code_on(client.get(resp["Location"]).content.decode())
    preview = ask(code, {"platform": "macos", **AGENT}, preview=True)
    assert "screen time" in preview["disclosure"]
    pc = pair_computer(device, code, **AGENT)
    device.refresh_from_db()
    assert device.person is None and device.controls_allowed
    assert pc.send(AGENT)["controls"] is not None
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert 'id="controls-title"' in page and reverse("devices:controls", args=[device.pk]) in page


def test_consent_names_activity_only_for_the_agent_on_a_controllable_computer(
    client, parent, family, sara
):
    client.force_login(parent)
    _, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    agent = ask(code, {"platform": "macos", **AGENT}, preview=True)["disclosure"]
    assert "screen time" in agent and "not pages or searches" in agent
    script = ask(code, {"platform": "macos"}, preview=True)["disclosure"]
    assert "screen time" not in script
    partner = Person.objects.create(account=family, kind="partner", name="Rahim")
    _, code = add_device(client, partner, platform="macos", name="Rahim's Mac")
    theirs = ask(code, {"platform": "macos", **AGENT}, preview=True)["disclosure"]
    assert "screen time" not in theirs


# ---------------------------------------------------------------- activity from the agent


def test_activity_adds_up_and_a_resent_batch_counts_once(client, mac):
    device, pc = mac
    turn_on(device)
    first = batch(
        screen=[{"day": today(), "user": "sara", "app": "Safari", "seconds": 600},
                {"day": today(), "user": "sara", "app": "Safari", "seconds": 60}],
        web=[{"day": today(), "user": "sara", "browser": "Chrome", "domain": "YouTube.com",
              "visits": 3, "seconds": 300}],
    )  # fmt: skip
    for _ in range(2):  # the answer was lost: the agent sends the same batch again
        pc.send({**AGENT, "activity": first})
    assert ScreenTime.objects.get().seconds == 660
    site = WebActivity.objects.get()
    assert (site.domain, site.visits, site.seconds) == ("youtube.com", 3, 300)
    pc.send({**AGENT, "activity": batch(
        screen=[{"day": today(), "user": "sara", "app": "Safari", "seconds": 120}],
        web=[{"day": today(), "user": "sara", "browser": "Chrome", "domain": "youtube.com",
              "visits": 1, "seconds": 30}])})  # fmt: skip
    assert ScreenTime.objects.get().seconds == 780
    site.refresh_from_db()
    assert (site.visits, site.seconds) == (4, 330)


def test_activity_is_dropped_when_switched_off_or_not_allowed(client, parent, family, mac):
    device, pc = mac
    row = [{"day": today(), "user": "sara", "app": "Safari", "seconds": 60}]
    pc.send({**AGENT, "activity": batch(screen=row)})  # no controls yet
    turn_on(device, screen_time=False)
    pc.send({**AGENT, "activity": batch(screen=row)})
    assert not ScreenTime.objects.exists()

    partner = Person.objects.create(account=family, kind="partner", name="Rahim")
    their_mac, pc = paired_mac(client, partner, name="Rahim's Mac")
    turn_on(their_mac)  # e.g. set before the device was reassigned
    pc.send({**AGENT, "activity": batch(screen=row)})
    assert not ScreenTime.objects.exists()
    assert not ActivityBatch.objects.filter(device=their_mac).exists()


def test_bad_activity_rows_are_dropped_not_the_check_in(client, mac):
    device, pc = mac
    turn_on(device)
    old = (timezone.localdate() - timedelta(days=400)).isoformat()
    pc.send({**AGENT, "activity": batch(
        screen=[{"day": old, "user": "sara", "app": "X", "seconds": 60},
                {"day": today(), "user": "", "app": "X", "seconds": 60},
                {"day": today(), "user": "sara", "app": "X", "seconds": 10**9},
                "nonsense"],
        web=[{"day": today(), "user": "sara", "domain": "https://evil.example/path?q=secret",
              "visits": 1},
             {"day": today(), "user": "sara", "domain": "not a domain", "visits": 1}])})  # fmt: skip
    assert list(ScreenTime.objects.values_list("seconds", flat=True)) == [86400]  # clamped
    assert list(WebActivity.objects.values_list("domain", flat=True)) == ["evil.example"]
    pc.send({**AGENT, "activity": "nope"})


def test_activity_is_pruned_after_retention(mac, settings):
    device, _ = mac
    settings.DEVICE_ACTIVITY_DAYS = 30
    old = timezone.localdate() - timedelta(days=31)
    ScreenTime.objects.create(device=device, day=old, os_user="sara", app="X", seconds=5)
    ScreenTime.objects.create(device=device, day=timezone.localdate(), os_user="sara", app="X",
                              seconds=5)  # fmt: skip
    assert activity.prune() == 1 and ScreenTime.objects.count() == 1


# ---------------------------------------------------------------- blocking


def test_blocklist_merges_adult_safe_search_and_the_parents_lists(client, mac):
    device, pc = mac
    blocklist.adult_path().write_text("pornsite.example\nallowed.example\n")
    blocklist.safe_search_path().write_text('{"forcesafesearch.google.com": "216.239.38.120"}')
    turn_on(device, block_adult=True, safe_search=True,
            blocked_domains="tiktok.com", allowed_domains="allowed.example")  # fmt: skip
    version = pc.send(AGENT)["controls"]["blocking"]
    assert version
    have, lines = pc.blocklist()  # left in the relay next to the settings, signed
    assert have == version
    assert "0.0.0.0 pornsite.example" in lines and "0.0.0.0 tiktok.com" in lines
    assert "0.0.0.0 www.tiktok.com" in lines  # a hosts file has no wildcards
    assert "216.239.38.120 www.google.com" in lines
    assert "0.0.0.0 use-application-dns.net" in lines  # keeps Firefox off DNS over HTTPS
    assert not any("allowed.example" in line for line in lines)
    # Applied: it says so, and the megabytes aren't written again.
    fake = relay.client()
    fake.strings.pop(pc.login["blocklist"])
    pc.send({**AGENT, "blocking_applied": version, "config": pc.settings()["digest"]})
    assert pc.blocklist() is None


def test_a_blocklist_not_yet_applied_is_written_once(client, mac):
    device, pc = mac
    turn_on(device, blocked_domains="tiktok.com")
    version = pc.send(AGENT)["controls"]["blocking"]
    relay.client().strings.pop(pc.login["blocklist"])
    pc.send({**AGENT, "blocking_applied": ""})  # still applying it: not sent again
    assert pc.blocklist() is None
    turn_on(device, blocked_domains="tiktok.com\nreddit.com")  # a new version is
    assert pc.send({**AGENT, "blocking_applied": version})["controls"]["blocking"] != version
    assert "0.0.0.0 reddit.com" in pc.blocklist()[1]


def test_saving_controls_updates_the_relay_at_once(client, mac):
    device, pc = mac
    pc.send(AGENT)
    resp = client.post(reverse("devices:controls", args=[device.pk]),
                       {"screen_time": "on", "blocked_domains": "tiktok.com"})  # fmt: skip
    assert resp.status_code == 302
    controls = pc.settings()["controls"]  # without waiting for a check-in
    assert controls["screen_time"] and controls["blocking"]
    assert pc.blocklist()[0] == controls["blocking"]


def test_blocking_off_sends_no_list(client, mac):
    device, pc = mac
    turn_on(device)
    assert pc.send(AGENT)["controls"]["blocking"] == ""
    assert pc.blocklist() is None


def test_a_failed_list_download_keeps_the_last_good_copy(settings, monkeypatch):
    settings.ADULT_BLOCKLIST_URL = "https://lists.example/hosts"
    blocklist.adult_path().write_text("kept.example\n")
    monkeypatch.setattr(blocklist, "_refresh_safe_search", lambda: False)

    def offline(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(blocklist.urllib.request, "urlopen", offline)
    assert blocklist.refresh(force=True) is False
    assert blocklist.adult()[0] == ("kept.example",)


def test_parse_hosts_reads_hosts_format():
    text = "# comment\n127.0.0.1 localhost\n0.0.0.0 a.example # x\n0.0.0.0 b.example c.example\n"
    assert blocklist.parse_hosts(text) == ["a.example", "b.example", "c.example"]


# ---------------------------------------------------------------- the pages


def test_parent_turns_controls_on_and_sees_activity(client, mac):
    device, pc = mac
    url = reverse("devices:controls", args=[device.pk])
    resp = client.post(url, {"screen_time": "on", "web_activity": "on", "block_adult": "on",
                             "blocked_domains": "https://www.TikTok.com/foo\nroblox.com"})  # fmt: skip
    assert resp.status_code == 302
    controls = DeviceControls.objects.get(device=device)
    assert controls.blocked_domains == "www.tiktok.com\nroblox.com" and controls.block_adult
    assert AuditLog.objects.filter(action="device.controls").exists()

    pc.send({**AGENT, "activity": batch(
        screen=[{"day": today(), "user": "sara", "app": "Minecraft", "seconds": 5400}],
        web=[{"day": today(), "user": "sara", "browser": "Chrome", "domain": "wikipedia.org",
              "visits": 7, "seconds": 900}])})  # fmt: skip
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "Minecraft" in page and "1h 30m" in page and "wikipedia.org" in page
    assert "Blocked" in page and "2 sites" in page


def test_bad_domains_are_refused_on_the_form(client, mac):
    device, _ = mac
    resp = client.post(reverse("devices:controls", args=[device.pk]),
                       {"blocked_domains": "not a site\nok.example"})  # fmt: skip
    assert resp.status_code == 200 and "Not a website address: not" in resp.content.decode()
    assert not DeviceControls.objects.exists()


def test_controls_cant_be_set_for_a_partners_computer(client, parent, family):
    client.force_login(parent)
    partner = Person.objects.create(account=family, kind="partner", name="Rahim")
    device, _ = paired_mac(client, partner, name="Rahim's Mac")
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert 'id="activity"' not in page and "staff member" in page
    client.post(reverse("devices:controls", args=[device.pk]), {"screen_time": "on"})
    assert not DeviceControls.objects.exists()


def test_another_account_cant_see_or_change_controls(client, mac, django_user_model):
    device, _ = mac
    other = make_account("Other family", kind="family")
    stranger = django_user_model.objects.create_user("x", "x@example.com", "pw-x-1234567")
    add_member(stranger, "owner", other)
    client.force_login(stranger)
    url = reverse("devices:controls", args=[device.pk])
    assert client.get(url).status_code == 404
    assert client.post(url, {"screen_time": "on"}).status_code == 404
    assert not DeviceControls.objects.exists()


def test_an_agent_without_admin_rights_gets_no_blocking(client, parent, sara):
    """Installed by the one-line command as the person: it can't edit the hosts file, so
    the dashboard never sends blocking, and the page doesn't offer it."""
    client.force_login(parent)
    device, code = add_device(client, sara, platform="windows", name="Sara's PC")
    user_agent = {**AGENT, "agent_mode": "user"}
    preview = ask(code, {"platform": "windows", **user_agent}, preview=True)
    assert "screen time" in preview["disclosure"] and "blocking" not in preview["disclosure"]
    pc = pair_computer(device, code, **user_agent)
    device.refresh_from_db()
    assert device.agent_mode == "user" and device.controls_allowed and not device.can_block
    DeviceControls.objects.create(device=device, screen_time=True, block_adult=True)

    controls = pc.send(user_agent)["controls"]
    assert controls["screen_time"] and controls["blocking"] == ""
    assert pc.blocklist() is None

    page = client.get(reverse("devices:controls", args=[device.pk])).content.decode()
    assert 'name="block_adult"' not in page and "without an administrator" in page
    client.post(reverse("devices:controls", args=[device.pk]), {"screen_time": "on"})
    assert DeviceControls.objects.get(device=device).block_adult  # left as it was

    # Installed again by an administrator: blocking comes back.
    system = {**AGENT, "agent_mode": "system"}
    assert pc.send(system)["controls"]["blocking"]


def test_browsers_the_mac_wont_let_the_agent_read_are_explained(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    pc = pair_computer(device, code, **AGENT)
    DeviceControls.objects.create(device=device, web_activity=True)
    pc.send({**AGENT, "web_no_access": ["Safari", "Chrome", 7, ""]})
    device.refresh_from_db()
    assert device.web_no_access == ["7", "Chrome", "Safari"]
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "Full Disk Access" in page and "/Library/Application Support/NexGuard/nexguard" in page
    pc.send({**AGENT, "web_no_access": []})  # access given
    device.refresh_from_db()
    assert device.web_no_access == []
    assert (
        "Full Disk Access"
        not in client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    )


# ------------------------------------------------------------------------- location


def _mac_with_location(client, parent, sara, on=True):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    pc = pair_computer(device, code, **AGENT)
    DeviceControls.objects.create(device=device, location=on)
    return device, pc


def test_a_computers_location_is_kept_only_when_switched_on(client, parent, sara):
    device, pc = _mac_with_location(client, parent, sara, on=False)
    fix = {"lat": 23.81, "lon": 90.41, "accuracy": 30, "source": "network"}
    resp = pc.send({**AGENT, "location": fix})
    assert resp["controls"]["location"] is False
    device.refresh_from_db()
    assert device.latitude is None and not device.locations.exists()

    DeviceControls.objects.filter(device=device).update(location=True)
    assert pc.send({**AGENT, "location": fix})["controls"]["location"]
    device.refresh_from_db()
    assert float(device.latitude) == 23.81 and device.location_accuracy_m == 30
    assert device.locations.get().source == "network"


def test_without_its_own_fix_a_computer_has_no_location(client, parent, sara, monkeypatch):
    """It checks in through the relay, so the dashboard never sees its internet address
    and can't place it by town: only the agent's own fix counts."""
    from apps.devices import geoip

    device, pc = _mac_with_location(client, parent, sara)
    seen = []
    monkeypatch.setattr(geoip, "locate", lambda ip: seen.append(ip) or
                        {"lat": 23.7, "lon": 90.4, "accuracy": 25000, "city": "Dhaka"})  # fmt: skip
    pc.send(AGENT)
    device.refresh_from_db()
    assert seen in ([], [None]) and device.latitude is None and not device.locations.exists()


def test_ip_location_never_for_a_computer_that_cant_have_controls(
    client, parent, sara, monkeypatch
):
    from apps.devices import geoip

    monkeypatch.setattr(geoip, "locate", lambda ip: {"lat": 1, "lon": 2, "accuracy": 25000})
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Shared Mac")
    pc = pair_computer(device, code)  # the script, no agent
    DeviceControls.objects.create(device=device, location=True)
    pc.send({"app_version": "nexguard-device-sh 1.2.0"})
    device.refresh_from_db()
    assert device.latitude is None


def test_geoip_lookups_are_local_and_safe(settings, tmp_path):
    from datetime import date

    from apps.devices import geoip

    assert geoip.locate("192.168.1.10") is None and geoip.locate("127.0.0.1") is None
    assert geoip.locate("not an ip") is None and geoip.locate(None) is None
    assert geoip.locate("8.8.8.8") is None  # no database downloaded yet
    settings.DEVICE_GEOIP_URL = "https://example.invalid/city-{month}.mmdb.gz"
    assert geoip._urls(date(2027, 1, 3)) == [
        "https://example.invalid/city-2027-01.mmdb.gz",
        "https://example.invalid/city-2026-12.mmdb.gz",
    ]
    assert geoip.refresh(force=True) is False and not geoip.path().exists()  # fetch failed
    settings.DEVICE_GEOIP_URL = ""
    assert geoip.refresh(force=True) is False


def test_controls_page_shows_what_each_switch_is_doing(client, parent, sara):
    device, pc = _mac_with_location(client, parent, sara)
    DeviceControls.objects.filter(device=device).update(screen_time=True)
    pc.send(AGENT)  # check-in after the save: in effect, no position
    url = reverse("devices:controls", args=[device.pk])
    page = client.get(url).content.decode()
    assert "In effect since the check-in" in page and "Nothing yet today" in page
    assert "can't place it by its internet address, and a Mac doesn't share its own" in page
    client.post(url, {"screen_time": "on"})  # saved after the check-in: not picked up yet
    assert "Waiting for Sara&#x27;s Mac to pick up" in client.get(url).content.decode()
