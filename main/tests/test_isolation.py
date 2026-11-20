"""Accounts can't see or touch each other. The release gate for multi-account installs.

Account B ("Globex") owns one of everything. An Admin of account A (Acme) tries every
page, form, API endpoint and background path that could reach it. Every attempt must
fail with 403/404 or simply not include B's data, and B's rows must stay untouched.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts import delivery, maintenance
from apps.alerts.models import Alert, AlertKind, Channel, MaintenanceWindow, Severity
from apps.api.models import ApiToken
from apps.audit.models import AuditLog
from apps.automations.models import Automation
from apps.devices.models import Device
from apps.fleet.models import Server, ServerGroup
from apps.monitors.models import Monitor
from apps.operations import services as ops
from apps.operations.models import Command, CommandStatus, Job
from apps.tenants.models import Account, Person
from apps.users.models import Invitation

from .conftest import acme, add_member, make_account, make_server, report

pytestmark = pytest.mark.django_db


@pytest.fixture
def globex(django_user_model):
    """Account B with one of everything, and its own admin."""
    b = make_account("Globex")
    boss = django_user_model.objects.create_user("globex-boss", "boss@globex.test", "pw-x-1234567")
    add_member(boss, "owner", b)
    server = make_server("globex-db", account=b)[0]
    Server.objects.filter(pk=server.pk).update(
        capabilities=report()["capabilities"], last_seen_at=timezone.now()
    )
    server.refresh_from_db()
    group = ServerGroup.objects.create(account=b, name="globex-group")
    server.groups.add(group)
    monitor = Monitor.objects.create(account=b, name="globex-shop", url="https://shop.globex.test/")
    channel = Channel.objects.create(account=b, name="globex-pager", kind="webhook",
                                     webhook_url="https://hooks.globex.test/x")  # fmt: skip
    now = timezone.now()
    window = MaintenanceWindow.objects.create(account=b, title="globex-upgrade", starts_at=now,
                                              ends_at=now + timedelta(hours=1))  # fmt: skip
    job = Job.objects.create(account=b, action="deploy_api", note="globex-deploy")
    command = Command.objects.create(job=job, server=server, action="deploy_api",
                                     deliver_by=now + timedelta(minutes=5))  # fmt: skip
    schedule = Automation.objects.create(account=b, name="globex-nightly", kind="action",
                                         action="deploy_api", cron="0 3 * * *")  # fmt: skip
    alert = Alert.objects.create(account=b, server=server, kind=AlertKind.OFFLINE,
                                 severity=Severity.CRITICAL, message="globex-db is offline")  # fmt: skip
    person = Person.objects.create(account=b, kind="staff", name="Globex Gary")
    device = Device.objects.create(account=b, person=person, platform="windows",
                                   name="globex-laptop", token_hash="globex-token")  # fmt: skip
    invite, _ = Invitation.issue(b, "new@globex.test", "viewer", boss)
    token, raw = ApiToken.issue(boss, b, "globex-ci", "operate")
    AuditLog.objects.create(account=b, action="server.create", target="globex-secret-audit")
    return {
        "account": b, "boss": boss, "server": server, "group": group, "monitor": monitor,
        "channel": channel, "window": window, "job": job, "command": command,
        "schedule": schedule, "alert": alert,
        "person": person, "invite": invite, "token": raw, "device": device,
    }  # fmt: skip


# Every URL that names one of B's objects. (url name, globex key, extra kwargs)
OBJECT_URLS = [
    ("fleet:server_detail", "server", {}),
    ("fleet:server_update", "server", {}),
    ("fleet:server_delete", "server", {}),
    ("fleet:server_enroll", "server", {}),
    ("fleet:server_rotate_token", "server", {}),
    ("fleet:server_upgrade", "server", {}),
    ("monitors:detail", "monitor", {}),
    ("monitors:update", "monitor", {}),
    ("monitors:delete", "monitor", {}),
    ("monitors:check", "monitor", {}),
    ("monitors:toggle", "monitor", {}),
    ("alerts:acknowledge", "alert", {}),
    ("alerts:channel_update", "channel", {}),
    ("alerts:channel_delete", "channel", {}),
    ("alerts:channel_test", "channel", {}),
    ("alerts:maintenance_end", "window", {}),
    ("alerts:maintenance_delete", "window", {}),
    ("operations:job_detail", "job", {}),
    ("operations:job_control", "job", {"op": "abort"}),
    ("operations:command_detail", "command", {}),
    ("operations:command_cancel", "command", {}),
    ("operations:command_stop", "command", {}),
    ("automations:detail", "schedule", {}),
    ("automations:update", "schedule", {}),
    ("automations:delete", "schedule", {}),
    ("automations:run_now", "schedule", {}),
    ("tenants:person_detail", "person", {}),
    ("tenants:person_update", "person", {}),
    ("devices:detail", "device", {}),
    ("devices:pair", "device", {}),
    ("devices:update", "device", {}),
    ("devices:delete", "device", {}),
    ("tenants:person_delete", "person", {}),
    ("users:invite_revoke", "invite", {}),
]


@pytest.mark.parametrize("url_name,key,extra", OBJECT_URLS)
def test_no_page_reaches_another_accounts_objects(client, admin_user, globex, url_name, key, extra):
    url = reverse(url_name, kwargs={"pk": globex[key].pk, **extra})
    client.force_login(admin_user)
    assert client.get(url).status_code in (403, 404, 405), url
    assert client.post(url, {"action": "remove"}).status_code in (403, 404, 405), url
    # Nothing was changed or removed.
    model = type(globex[key])
    fresh = model.objects.get(pk=globex[key].pk)
    if hasattr(fresh, "is_enabled"):
        assert fresh.is_enabled
    if isinstance(fresh, Command):
        assert fresh.status == CommandStatus.PENDING
    if isinstance(fresh, Invitation):
        assert fresh.revoked_at is None
    if isinstance(fresh, Device):
        assert fresh.pairing_code_hash == "" and fresh.token_hash == "globex-token"


def test_member_pages_only_show_this_account(client, admin_user, globex):
    client.force_login(admin_user)
    assert client.get(reverse("users:detail", args=[globex["boss"].pk])).status_code == 404
    assert client.post(reverse("users:detail", args=[globex["boss"].pk]),
                       {"action": "reset_link"}).status_code == 404  # fmt: skip
    assert b"globex-boss" not in client.get(reverse("users:list")).content
    assert b"new@globex.test" not in client.get(reverse("users:list")).content


LIST_PAGES = [
    ("fleet:dashboard", "globex-db"),
    ("monitors:list", "globex-shop"),
    ("alerts:list", "globex-db is offline"),
    ("alerts:channels", "globex-pager"),
    ("alerts:maintenance", "globex-upgrade"),
    ("operations:job_list", "globex-deploy"),
    ("automations:list", "globex-nightly"),
    ("operations:job_create", "globex-db"),
    ("automations:create", "globex-group"),
    ("monitors:create", "globex-db"),
    ("alerts:maintenance_create", "globex-db"),
    ("tenants:people", "Globex Gary"),
    ("tenants:people", "globex-laptop"),
    ("devices:create", "Globex Gary"),
    ("audit:list", "globex-secret-audit"),
]


@pytest.mark.parametrize("url_name,secret", LIST_PAGES)
def test_lists_and_forms_never_show_another_account(client, admin_user, globex, url_name, secret):
    make_server("acme-web")
    client.force_login(admin_user)
    query = {"kind": "server"} if url_name == "fleet:dashboard" else {}
    resp = client.get(reverse(url_name), query)
    assert resp.status_code == 200
    assert secret not in resp.content.decode()


def test_forms_refuse_another_accounts_ids(client, admin_user, globex):
    mine = make_server("acme-web")[0]
    client.force_login(admin_user)
    # Run a job on B's server.
    client.post(reverse("operations:job_create"),
                {"action": "deploy_api", "servers": [str(globex["server"].pk)]})  # fmt: skip
    assert not Job.objects.filter(account=acme()).exists()
    # Mute B's server, or mute "everything" and hope it reaches B.
    now = timezone.localtime()
    client.post(reverse("alerts:maintenance_create"), {
        "title": "sneaky", "starts_at": now.strftime("%Y-%m-%dT%H:%M"),
        "ends_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "servers": [str(globex["server"].pk)],
    })  # fmt: skip
    assert not MaintenanceWindow.objects.filter(title="sneaky").exists()
    client.post(reverse("alerts:quick_mute"), {"server": str(globex["server"].pk), "minutes": 60})
    assert not MaintenanceWindow.objects.filter(account=acme()).exists()
    # A monitor checked from B's server, or attached to it.
    client.post(reverse("monitors:create"), {
        "kind": "http", "name": "probe-b", "url": "https://x.test/", "method": "GET",
        "interval_seconds": 60, "timeout_seconds": 10, "expected_status": "200-399",
        "failure_threshold": 2, "probe_servers": [str(globex["server"].pk)], "quorum": 1,
        "server": str(globex["server"].pk), "is_enabled": "on",
    })  # fmt: skip
    assert not Monitor.objects.filter(name="probe-b").exists()
    # A schedule on B's group.
    client.post(reverse("automations:create"), {
        "name": "b-group", "kind": "action", "cron": "0 3 * * *", "action": "deploy_api",
        "groups": [str(globex["group"].pk)], "is_enabled": "on", "interpreter": "bash",
    })  # fmt: skip
    assert not Automation.objects.filter(name="b-group").exists()
    # Names are unique per account only: Acme may have its own "globex-db".
    assert client.get(reverse("fleet:server_detail", args=[mine.pk])).status_code == 200


def test_schedule_environments_resolve_inside_the_account(globex):
    mine = make_server("acme-prod", environment="production")[0]
    sched = Automation.objects.create(account=acme(), name="nightly", kind="action",
                                      action="deploy_api", cron="0 3 * * *",
                                      environments=["production"])  # fmt: skip
    assert list(sched.targets()) == [mine]  # not globex-db, also "production"


def test_jobs_refuse_servers_of_another_account(admin_user, globex):
    mine = make_server("acme-web")[0]
    with pytest.raises(ops.JobError, match="aren't in this account"):
        ops.create_job(admin_user, "deploy_api", {}, [mine, globex["server"]], account=acme())
    with pytest.raises(ops.JobError, match="no longer a member"):
        ops.create_job(admin_user, "deploy_api", {}, [globex["server"]])


def test_api_token_sees_only_its_account(client, admin_user, globex):
    _, raw = ApiToken.issue(admin_user, acme(), "ci", "operate")
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    for name in ("api_servers", "api_monitors", "api_alerts", "api_jobs"):
        body = client.get(reverse(name), **auth).content.decode()
        assert "globex" not in body, name
    for name, key in (("api_server", "server"), ("api_monitor", "monitor"),
                      ("api_job", "job"), ("api_command", "command")):  # fmt: skip
        assert client.get(reverse(name, args=[globex[key].pk]), **auth).status_code == 404
    resp = client.post(reverse("api_jobs"), {"action": "deploy_api", "servers": ["globex-db"]},
                       content_type="application/json", **auth)  # fmt: skip
    assert resp.status_code == 400 and not Job.objects.filter(account=acme()).exists()
    assert "globex" not in client.get(reverse("metrics"), **auth).content.decode()


def test_api_token_stops_when_its_owner_leaves_the_account(client, globex):
    auth = {"HTTP_AUTHORIZATION": f"Bearer {globex['token']}"}
    assert client.get(reverse("api_servers"), **auth).status_code == 200
    globex["account"].memberships.filter(user=globex["boss"]).delete()
    assert client.get(reverse("api_servers"), **auth).status_code == 403


def test_alerts_only_reach_their_own_accounts_channels(monkeypatch, globex):
    sent = []
    monkeypatch.setattr(delivery, "send", lambda channel, *a, **kw: sent.append(channel.name))
    Channel.objects.create(account=acme(), name="acme-pager", kind="webhook",
                           webhook_url="https://hooks.acme.test/x")  # fmt: skip
    Channel.objects.update(created_at=timezone.now() - timedelta(hours=1))
    MaintenanceWindow.objects.filter(pk=globex["window"].pk).delete()
    delivery.process()
    assert sent == ["globex-pager"]


def test_muting_everything_stays_inside_the_account(globex):
    MaintenanceWindow.objects.filter(pk=globex["window"].pk).delete()
    now = timezone.now()
    MaintenanceWindow.objects.create(account=acme(), title="all", all_targets=True,
                                     starts_at=now, ends_at=now + timedelta(hours=1))  # fmt: skip
    assert not maintenance.coverage().covers_alert(globex["alert"])
    assert maintenance.coverage(account=acme()).windows


def test_switching_needs_a_membership(client, admin_user, globex):
    client.force_login(admin_user)
    resp = client.post(reverse("tenants:switch"), {"account": str(globex["account"].pk)})
    assert resp.status_code == 404
    # A stale session pointing at B (removed member) falls back to an account they're in.
    session = client.session
    session["account_id"] = str(globex["account"].pk)
    session.save()
    assert b"globex-db" not in client.get(reverse("fleet:dashboard"), {"kind": "server"}).content


def test_a_user_in_two_accounts_sees_each_separately(client, admin_user, globex):
    add_member(admin_user, "viewer", globex["account"])
    make_server("acme-web")
    client.force_login(admin_user)
    assert b"acme-web" in client.get(reverse("fleet:dashboard"), {"kind": "server"}).content
    client.post(reverse("tenants:switch"), {"account": str(globex["account"].pk)})
    page = client.get(reverse("fleet:dashboard"), {"kind": "server"}).content
    assert b"globex-db" in page and b"acme-web" not in page
    # A viewer there, whatever they are at Acme.
    assert client.get(reverse("operations:job_create")).status_code == 403
    assert client.get(reverse("users:list")).status_code == 403


def test_probe_results_and_pings_stay_inside_the_account(globex):
    from apps.monitors import checker

    mine = make_server("acme-web")[0]
    # Even if a B monitor somehow listed an Acme server, the agent gets no assignment.
    globex["monitor"].probe_servers.add(mine)
    assert checker.probe_assignments(mine) == []
    stored = checker.receive_probe_results(mine, [{"id": str(globex["monitor"].pk), "ok": True}])
    assert stored == 0


# ---------------------------------------------------------------- account types


@pytest.fixture
def family_owner(django_user_model):
    account = make_account("The Rahmans", kind="family")
    user = django_user_model.objects.create_user("mum", "mum@example.com", "pw-x-1234567")
    add_member(user, "owner", account)
    return user


@pytest.mark.parametrize(
    "url_name",
    ["fleet:dashboard", "fleet:server_create", "monitors:list", "operations:job_list",
     "operations:job_create", "alerts:maintenance", "api_tokens"],
)  # fmt: skip
def test_family_accounts_have_no_infrastructure(client, family_owner, url_name):
    client.force_login(family_owner)
    assert client.get(reverse(url_name)).status_code in (403, 404)


def test_family_home_is_its_people(client, family_owner):
    client.force_login(family_owner)
    assert client.get(reverse("home"))["Location"] == reverse("tenants:people")
    page = client.get(reverse("tenants:people")).content.decode()
    assert "People" in page and "Add person" in page and "Servers" not in page
    client.post(
        reverse("tenants:person_create"), {"kind": "child", "name": "Sara", "notes": "iPad"}
    )
    (sara,) = Person.objects.all()
    assert sara.kind == "child" and sara.account.kind == "family"
    resp = client.post(reverse("tenants:person_create"), {"kind": "child", "name": "sara"})
    assert "already has that name" in resp.content.decode()
    # Family owners don't have to use 2FA (they can't run anything on servers).
    from apps.users import twofactor

    assert not twofactor.is_required(family_owner)


def test_solo_accounts_have_no_team_or_people(client, django_user_model, settings):
    settings.REQUIRE_2FA_FOR_ADMINS = True
    account = make_account("Me", kind="solo")
    dev = django_user_model.objects.create_user("dev", "dev@example.com", "pw-x-1234567")
    add_member(dev, "owner", account)
    from apps.users import twofactor

    assert twofactor.is_required(dev)  # can deploy to servers: 2FA as for admins
    settings.REQUIRE_2FA_FOR_ADMINS = False
    client.force_login(dev)
    assert client.get(reverse("fleet:dashboard"), {"kind": "server"}).status_code == 200
    assert client.get(reverse("api_tokens")).status_code == 200
    for name in ("users:list", "users:invite", "tenants:people"):
        assert client.get(reverse(name)).status_code == 403


def test_new_account_needs_signup_or_superuser(client, admin_user, settings):
    client.force_login(admin_user)
    data = {"kind": "family", "account_name": "Home"}
    settings.ALLOW_SIGNUP = False
    assert client.post(reverse("tenants:create"), data).status_code == 403
    settings.ALLOW_SIGNUP = True
    assert client.post(reverse("tenants:create"), data)["Location"] == reverse("home")
    home = Account.objects.get(name="Home")
    assert home.memberships.get().user == admin_user and home.memberships.get().role == "owner"


def test_login_without_an_account_is_sent_to_create_one(client, django_user_model):
    user = django_user_model.objects.create_user("lost", "lost@example.com", "pw-x-1234567")
    client.force_login(user)
    assert client.get(reverse("fleet:dashboard"), {"kind": "server"})["Location"] == reverse(
        "tenants:create"
    )
    assert client.get(reverse("account")).status_code == 200  # own login still manageable


def test_solo_accounts_call_servers_devices_and_have_no_maintenance(client, django_user_model):
    from apps.tenants.models import Kind

    account = make_account("Just me", kind=Kind.SOLO)
    dev = django_user_model.objects.create_user("dev", "dev@example.com", "pw-x-1234567")
    add_member(dev, "owner", account)
    client.force_login(dev)
    page = client.get(reverse("fleet:dashboard"), {"kind": "device"}).content.decode()
    assert '<h1 class="page-title">Devices</h1>' in page and "Add device" in page
    assert '<span class="side-text">Devices</span>' in page
    assert '<span class="side-text">Applications</span>' in page
    assert "Maintenance" not in page and reverse("alerts:maintenance") not in page
    assert client.get(reverse("alerts:maintenance")).status_code in (403, 404)


def test_companies_still_say_servers_and_keep_maintenance(client, admin_user):
    client.force_login(admin_user)
    page = client.get(reverse("fleet:dashboard"), {"kind": "server"}).content.decode()
    assert '<h1 class="page-title">Servers</h1>' in page and "Maintenance" in page
