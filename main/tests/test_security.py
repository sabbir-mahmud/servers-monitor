"""Regression tests for the security review. Each test is one attack that used to work;
it must now fail. Numbers match the review findings."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.fleet.models import MetricSample

from .conftest import report

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def owner():
    return User.objects.create_superuser("owner", "owner@example.com", "owner-secret-pass-1")


# 1 — an Admin could take over the superuser --------------------------------------------


def test_1_admin_cannot_reset_reroute_or_deactivate_a_superuser(client, admin_user, owner):
    client.force_login(admin_user)
    for data in (
        {"action": "reset_link"},
        {"action": "deactivate"},
        {
            "action": "save",
            "first_name": "",
            "last_name": "",
            "email": "attacker@evil.test",
            "role": "Admin",
        },
    ):
        resp = client.post(reverse("users:detail", args=[owner.pk]), data)
        assert b"/password/reset/" not in resp.content
    owner.refresh_from_db()
    assert owner.is_active and owner.email == "owner@example.com"
    page = client.get(reverse("users:detail", args=[owner.pk])).content.decode()
    assert "Only another superuser can change it" in page and 'value="reset_link"' not in page


def test_1_superusers_can_still_manage_each_other(client, owner):
    other = User.objects.create_superuser("second", "s@example.com", "second-secret-pass-1")
    client.force_login(owner)
    page = client.post(reverse("users:detail", args=[other.pk]), {"action": "reset_link"}).content
    assert b"/password/reset/" in page


# 2 — Django admin login had no throttle -----------------------------------------------


def test_2_admin_login_goes_through_the_throttled_login(client, owner, settings):
    resp = client.get("/admin/login/?next=/admin/")
    assert resp.status_code == 302 and resp["Location"].startswith(reverse("login"))
    settings.LOGIN_FAILURE_LIMIT = 3
    for _ in range(3):
        client.post("/admin/login/", {"username": "owner", "password": "wrong"})
        client.post(reverse("login"), {"username": "owner", "password": "wrong"})
    resp = client.post(reverse("login"), {"username": "owner", "password": "owner-secret-pass-1"})
    assert b"Too many failed attempts" in resp.content


def test_2_signed_in_non_staff_gets_403_not_a_loop(client, viewer):
    client.force_login(viewer)
    assert client.get("/admin/login/").status_code == 403


# 5 — throttle bypass by switching username/email, or IPs --------------------------------


def test_5_username_and_email_share_one_counter(client, roles, settings):
    settings.LOGIN_FAILURE_LIMIT = 3
    user = User.objects.create_user("sabbir", "sabbir@example.com", "the-real-password-1")
    user.groups.add(Group.objects.get(name="Viewer"))
    for _ in range(3):
        client.post(reverse("login"), {"username": "sabbir", "password": "guess"})
    resp = client.post(
        reverse("login"), {"username": "sabbir@example.com", "password": "the-real-password-1"}
    )
    assert resp.status_code == 200 and b"Too many failed attempts" in resp.content


def test_5_rotating_ips_hits_the_per_account_limit(client, roles, settings):
    settings.LOGIN_ACCOUNT_FAILURE_LIMIT = 5
    User.objects.create_user("sabbir", "s@example.com", "the-real-password-1")
    for i in range(5):
        client.post(
            reverse("login"), {"username": "sabbir", "password": "x"}, REMOTE_ADDR=f"10.0.0.{i}"
        )
    resp = client.post(
        reverse("login"),
        {"username": "sabbir", "password": "the-real-password-1"},
        REMOTE_ADDR="10.9.9.9",
    )
    assert b"Too many failed attempts" in resp.content


# 6 — cookies over plain HTTP ------------------------------------------------------------


def test_6_secure_cookies_by_default_in_production():
    import subprocess
    import sys

    code = (
        "import django,os;os.environ.update(DEBUG='False',SECRET_KEY='x'*50,REDIS_URL='rediss://a@b:1',"
        "DJANGO_SETTINGS_MODULE='config.settings');os.environ.pop('HTTPS',None);django.setup();"
        "from django.conf import settings as s;"
        "print(s.HTTPS, s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_PROXY_SSL_HEADER is not None)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="."
    ).stdout
    assert out.split() == ["True", "True", "True", "False"]  # no proxy header unless trusted


def test_6_admins_warned_on_plain_http_from_another_machine(client, admin_user):
    client.force_login(admin_user)
    remote = client.get(reverse("fleet:dashboard"), REMOTE_ADDR="192.168.1.20").content
    local = client.get(reverse("fleet:dashboard"), REMOTE_ADDR="127.0.0.1").content
    assert b"served over plain HTTP" in remote and b"served over plain HTTP" not in local


# 7 — open redirects ---------------------------------------------------------------------


def test_7_next_cannot_leave_the_site(client, operator):
    from apps.alerts.models import Alert, AlertKind

    from .conftest import make_server

    server, _ = make_server("web-01")
    alert = Alert.objects.create(
        server=server, kind=AlertKind.OFFLINE, severity="critical", message="x"
    )
    client.force_login(operator)
    for evil in ("https://evil.example/phish", "//evil.example", "javascript:alert(1)"):
        resp = client.post(reverse("alerts:acknowledge", args=[alert.pk]), {"next": evil})
        assert resp["Location"] == reverse("alerts:list")
    ok = client.post(reverse("alerts:acknowledge", args=[alert.pk]), {"next": "/servers/"})
    assert ok["Location"] == "/servers/"


# 8 — email change without password ------------------------------------------------------


def test_8_changing_email_needs_the_current_password(
    client, roles, settings, mailoutbox, monkeypatch
):
    from apps.users import accounts

    monkeypatch.setattr(accounts, "send_async", accounts._send)
    settings.EMAIL_ENABLED = True
    user = User.objects.create_user("me", "me@example.com", "my-real-password-1")
    client.force_login(user)
    client.post(
        reverse("account"), {"first_name": "", "last_name": "", "email": "attacker@evil.test"}
    )
    user.refresh_from_db()
    assert user.email == "me@example.com"
    client.post(reverse("account"), {"first_name": "", "last_name": "", "email": "new@example.com",
                                     "current_password": "my-real-password-1"})  # fmt: skip
    user.refresh_from_db()
    assert user.email == "new@example.com"
    assert mailoutbox[-1].to == ["me@example.com"]  # the old address is told
    client.post(
        reverse("account"), {"first_name": "Me", "last_name": "", "email": "new@example.com"}
    )
    user.refresh_from_db()
    assert user.first_name == "Me"  # names don't need the password


# 9 — SSRF from health checks and webhooks -----------------------------------------------


class _Secret(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"internal-admin-panel token=abc"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


@pytest.fixture
def internal_site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Secret)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_9_checks_cannot_reach_internal_addresses(internal_site):
    from apps.monitors import checker
    from apps.monitors.models import Monitor

    result = checker.probe(
        Monitor(name="p", url=internal_site, keyword="token=abc", timeout_seconds=3)
    )
    assert not result.ok and result.error.startswith("blocked:")


def test_9_opt_in_allows_lan_but_never_metadata(internal_site, settings):
    from apps.core import netguard
    from apps.monitors import checker
    from apps.monitors.models import Monitor

    settings.MONITOR_ALLOW_PRIVATE_TARGETS = True
    assert checker.probe(Monitor(name="p", url=internal_site, timeout_seconds=3)).ok
    assert not netguard.is_allowed("169.254.169.254")  # cloud metadata, always
    assert not netguard.is_allowed("::ffff:169.254.169.254")
    assert netguard.is_allowed("1.1.1.1")


def test_9_redirects_are_rechecked():
    import socket

    from apps.core import netguard
    from apps.monitors import checker
    from apps.monitors.models import Monitor

    class Hop(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Hop)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    # Pretend the first hop is public so only the redirect target can be blocked.
    real = netguard.is_allowed
    netguard.is_allowed = lambda ip: ip == "127.0.0.1" or real(ip)
    try:
        result = checker.probe(
            Monitor(name="r", url=f"http://127.0.0.1:{port}/", timeout_seconds=3)
        )
    finally:
        netguard.is_allowed = real
        httpd.shutdown()
    assert not result.ok and ("blocked" in result.error or "timed out" in result.error)
    del socket


def test_9_forms_refuse_internal_urls(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("monitors:create"), {
        "name": "x", "url": "http://127.0.0.1:8000/", "method": "GET", "interval_seconds": 60,
        "timeout_seconds": 10, "expected_status": "200", "failure_threshold": 2,
    })  # fmt: skip
    assert b"This host is blocked" in resp.content
    resp = client.post(reverse("alerts:channel_create") + "?kind=webhook", {
        "kind": "webhook", "name": "w", "webhook_url": "https://127.0.0.1/hook", "min_severity": "warning",
        "include_servers": "on", "repeat_minutes": "0",
    })  # fmt: skip
    assert b"This host is blocked" in resp.content


def test_9_webhooks_are_guarded_at_send_time(internal_site):
    from apps.alerts import delivery

    with pytest.raises(delivery.SendError, match="blocked"):
        delivery.webhook_send(internal_site, "hi")


# 12 — a compromised agent flooding the database -----------------------------------------


def test_12_flood_of_heartbeats_stores_one(agent, server_and_token):
    from apps.agent_api.bridge import handle

    agent.heartbeat()
    for _ in range(500):
        handle(server_and_token[0], {"t": "hb", "interval": 120, "report": report()})
    assert MetricSample.objects.filter(server=server_and_token[0]).count() == 1


# 13 — secrets at rest -------------------------------------------------------------------


def test_13_channel_secrets_are_encrypted_in_the_database():
    from django.db import connection

    from apps.alerts.models import Channel

    c = Channel.objects.create(
        name="tg", kind="telegram", telegram_token="123:SECRET", telegram_chat_id="1"
    )
    with connection.cursor() as cur:
        cur.execute("SELECT telegram_token FROM alerts_channel WHERE id = %s", [c.pk.hex])
        stored = cur.fetchone()[0]
    assert stored.startswith("enc1:") and "SECRET" not in stored
    assert Channel.objects.get(pk=c.pk).telegram_token == "123:SECRET"


def test_13_key_rotation_with_fallback_still_decrypts(settings):
    from apps.core import crypto

    token = crypto.encrypt("hello")
    old = settings.SECRET_KEY
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = "n" * 50, [old]
    assert crypto.decrypt(token) == "hello"
    settings.SECRET_KEY_FALLBACKS = []
    assert crypto.decrypt(token) == ""  # never returns ciphertext as if it were the secret


def test_13_enrolment_credential_is_encrypted_in_the_session(client, admin_user, fake_relay):
    client.force_login(admin_user)
    client.post(reverse("fleet:server_create"), {"name": "api-9", "environment": "production",
                                                 "report_interval": 120, "is_enabled": "on"})  # fmt: skip
    stored = [v for k, v in client.session.items() if k.startswith("enroll-token:")]
    assert stored and stored[0].startswith("enc1:") and "rediss://" not in stored[0]


# 4 — verified installs ------------------------------------------------------------------


def test_4_install_command_pins_checksums_and_keeps_secrets_out(client, admin_user, fake_relay):
    from apps.fleet import agentdist

    client.force_login(admin_user)
    resp = client.post(reverse("fleet:server_create"), {"name": "api-8", "environment": "production",
                                                        "report_interval": 120, "is_enabled": "on"})  # fmt: skip
    page = client.get(resp["Location"]).content.decode()
    command = page.split('id="install">')[1].split("</pre>")[0]
    assert "sha256sum -c" in command and agentdist.sums()["install"] in command
    assert "SM_SUMS=agent=" in command and "rediss://" not in command  # credential not in argv


def test_netguard_refuses_internal_addresses_before_connecting(settings, monkeypatch):
    """No connection attempt at all, so open and closed internal ports look the same."""
    import socket

    from apps.core import netguard

    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("connected"))
    for host in ("127.0.0.1", "10.0.0.8", "169.254.169.254", "localhost"):
        with pytest.raises(netguard.BlockedAddress):
            netguard.create_connection((host, 6379), timeout=1)


# Phase 3–4 surfaces: each one an attack that must fail ---------------------------------


def test_api_ignores_the_browser_session(client, admin_user, agent):
    """A malicious page can make a signed-in browser POST to the dashboard (CSRF). The API
    is CSRF-exempt, so it must never accept the session cookie as a credential."""
    agent.heartbeat()
    client.force_login(admin_user)
    r = client.post(reverse("api_jobs"), '{"action": "deploy_api", "servers": ["web-01"]}',
                    content_type="application/json")  # fmt: skip
    assert r.status_code == 401
    from apps.operations.models import Job

    assert not Job.objects.exists()


def test_api_token_cannot_reach_browser_only_shell(client, admin_user, agent):
    from apps.api.models import ApiToken, Scope

    agent.heartbeat(report(capabilities={"shell": True, "actions": [], "logs": []}))
    raw = ApiToken.issue(admin_user, "leaked", Scope.OPERATE)[1]
    r = client.post(reverse("api_jobs"), '{"action": "shell", "args": {"command": "id"}, "servers": ["web-01"]}',
                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")  # fmt: skip
    assert r.status_code == 403


def test_agent_cannot_write_probe_results_for_monitors_it_was_not_given(fake_relay):
    from apps.monitors.models import Monitor, MonitorCheck

    from .conftest import AgentClient, make_server

    server = make_server("probe", signing_salt="ef" * 16)[0]
    victim = Monitor.objects.create(name="payments", url="https://pay.example.com/")
    AgentClient(server, fake_relay).heartbeat(report(
        capabilities={"features": ["probes"]},
        probe_results=[{"id": str(victim.pk), "ok": False, "error": "forged outage"}]))  # fmt: skip
    assert not MonitorCheck.objects.filter(monitor=victim).exists()


def test_sso_never_links_a_superuser_or_takes_over_by_unverified_email(settings, roles):
    """A provider account with someone's email must not become them unless the provider
    verified it, and never a superuser (local break-glass only)."""
    from apps.users.sso import SSOBackend

    settings.OIDC_OP_TOKEN_ENDPOINT = settings.OIDC_OP_USER_ENDPOINT = "https://idp.example.com/x"
    settings.OIDC_OP_JWKS_ENDPOINT = "https://idp.example.com/jwks"
    settings.OIDC_RP_CLIENT_ID = settings.OIDC_RP_CLIENT_SECRET = "x"
    settings.OIDC_DEFAULT_ROLE = "Viewer"
    backend = SSOBackend()
    claims = {"sub": "evil", "email": "owner@example.com", "groups": []}
    assert not backend.verify_claims({**claims, "email_verified": False})
    root = User.objects.create_superuser("root", "owner@example.com", "pw-1-not-used!")
    from django.core.exceptions import SuspiciousOperation

    with pytest.raises(SuspiciousOperation):
        backend.update_user(root, {**claims, "email_verified": True})
