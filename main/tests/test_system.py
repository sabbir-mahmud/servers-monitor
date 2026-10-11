"""Phase 1: the dashboard watching itself (watchdog, downtime, System page)."""

import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts import delivery
from apps.alerts.models import Channel, Delivery, DeliveryEvent
from apps.audit.models import AuditLog
from apps.core import system
from apps.core.models import SystemState

pytestmark = pytest.mark.django_db


class _Ping(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        _Ping.hits += 1
        self.send_response(200 if "ok" in self.path else 500)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *a):
        pass


@pytest.fixture
def ping_server(settings):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = True  # the fake service lives on 127.0.0.1
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_watchdog_pings_and_records(ping_server, settings):
    settings.WATCHDOG_URL = f"{ping_server}/ok"
    state = system.ping_watchdog()
    assert state["ok"] and SystemState.get("watchdog")["ok"]
    settings.WATCHDOG_URL = f"{ping_server}/fail"
    assert not system.ping_watchdog()["ok"]


def test_watchdog_off_when_not_configured(settings):
    settings.WATCHDOG_URL = ""
    assert system.ping_watchdog() is None


def test_downtime_gap_notifies_every_channel_once(monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send", lambda ch, msg, *a: sent.append((ch.name, msg.plain)))
    Channel.objects.create(name="phone", kind="telegram", telegram_token="t", telegram_chat_id="1")
    Channel.objects.create(
        name="off", kind="telegram", telegram_token="t", telegram_chat_id="1", is_enabled=False
    )
    now = timezone.now()
    gap = system.check_downtime(now - timedelta(hours=2, minutes=21), now=now)
    assert gap and len(sent) == 1 and "2 h 21 min" in sent[0][1]
    assert Delivery.objects.get().event == DeliveryEvent.SYSTEM
    assert AuditLog.objects.filter(action="system.downtime").count() == 1
    assert SystemState.get("last_downtime")["minutes"] == 141


def test_short_gaps_are_not_downtime(monkeypatch):
    monkeypatch.setattr(delivery, "send", lambda *a: pytest.fail("should not notify"))
    now = timezone.now()
    assert system.check_downtime(now - timedelta(minutes=2), now=now) is None
    assert system.check_downtime(None) is None


def test_system_page_admin_only(client, viewer, admin_user, settings):
    settings.WATCHDOG_URL = ""
    client.force_login(viewer)
    assert client.get(reverse("system")).status_code == 403
    client.force_login(admin_user)
    page = client.get(reverse("system")).content.decode()
    assert "Watchdog" in page and "Not set up" in page and settings.APP_VERSION in page


# ---------------------------------------------------------------- backups (1.2)


@pytest.fixture
def live_db(tmp_path, settings):
    """A real on-disk SQLite db (the test database is in memory)."""
    import sqlite3

    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT);"
        "CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO django_migrations (app) VALUES ('core');"
        "INSERT INTO things (name) VALUES ('a'), ('b'), ('c');"
    )
    conn.commit()
    conn.close()
    settings.SQLITE_PATH = path
    settings.BACKUP_DIR = str(tmp_path / "backups")
    return path


def test_snapshot_is_a_consistent_copy_and_rotates(live_db, settings):
    import sqlite3

    from apps.core import backup

    settings.BACKUP_KEEP = 2
    states = [backup.take_snapshot() for _ in range(3)]
    assert all(s["ok"] for s in states)
    snaps = backup.snapshots()
    assert len(snaps) <= 2 and SystemState.get("backup")["count"] == len(snaps)
    rows = sqlite3.connect(snaps[-1]).execute("SELECT count(*) FROM things").fetchone()[0]
    assert rows == 3 and backup.check_integrity(snaps[-1]) == "ok"


def test_snapshot_due_and_stale(live_db):
    from apps.core import backup

    assert backup.snapshot_due()
    backup.take_snapshot()
    assert not backup.snapshot_due()
    later = timezone.now() + timedelta(hours=25)
    assert backup.snapshot_due(later)
    assert not backup.is_stale(SystemState.get("backup"))
    assert backup.is_stale(SystemState.get("backup"), timezone.now() + timedelta(hours=37))


def test_restore_refuses_running_relay_and_corrupt_files(live_db, tmp_path, monkeypatch):
    from django.core.management import CommandError, call_command

    from apps.agent_api import relay
    from apps.core import backup

    snap = backup.take_snapshot()["path"]
    monkeypatch.setattr(relay, "is_alive", lambda: True)
    with pytest.raises(CommandError, match="relay process is running"):
        call_command("restore", snap)
    monkeypatch.setattr(relay, "is_alive", lambda: False)
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database")
    with pytest.raises(CommandError, match="integrity"):
        call_command("restore", str(bad))
    call_command("restore", snap)  # good snapshot: restores, keeps the old db
    assert list(live_db.parent.glob("db.before-restore-*.sqlite3"))


def test_system_page_flags_stale_backup(client, admin_user, live_db):
    from apps.core import backup

    backup.take_snapshot()
    state = SystemState.get("backup")
    state["at"] = (timezone.now() - timedelta(days=3)).isoformat()
    SystemState.put("backup", state)
    client.force_login(admin_user)
    assert "over 36 h old" in client.get(reverse("system")).content.decode()


# ---------------------------------------------------------------- Redis usage (1.4)


def test_counting_client_counts_every_command(monkeypatch):
    from apps.agent_api import relay, usage

    monkeypatch.setattr("redis.Redis.execute_command", lambda self, *a, **k: "PONG")
    monkeypatch.setitem(usage._pending, "dashboard", 0)
    r = relay._CountingRedis.from_url("redis://127.0.0.1:1/0")
    r.ping()
    r.execute_command("LLEN", "x")
    assert usage._pending["dashboard"] == 2


def test_usage_flush_and_projection(settings, monkeypatch):
    from datetime import date

    from apps.agent_api import usage
    from apps.core.models import RelayUsage

    monkeypatch.setitem(usage._pending, "dashboard", 0)
    monkeypatch.setitem(usage._pending, "agent", 0.0)
    settings.RELAY_FREE_COMMANDS, settings.RELAY_PRICE_PER_100K = 500_000, 0.2
    usage.count_dashboard(100)
    for _ in range(10):
        usage.count_agent_message("hb")  # 2.02 each
    usage.flush(date(2026, 10, 1))
    usage.count_dashboard(50)
    usage.flush(date(2026, 10, 1))  # adds to the same day
    row = RelayUsage.objects.get(day=date(2026, 10, 1))
    assert (row.dashboard, row.agent) == (150, 20)
    RelayUsage.objects.create(day=date(2026, 10, 10), dashboard=40_000, agent=0)
    s = usage.month_summary(date(2026, 10, 10))
    # 40,170 over 10 days → 4,017/day × 31 days
    assert s["so_far"] == 40_170 and s["projected"] == 124_527 and s["cost"] == 0 and not s["warn"]
    RelayUsage.objects.create(day=date(2026, 10, 5), dashboard=300_000, agent=0)
    s = usage.month_summary(date(2026, 10, 10))
    assert s["projected"] > 500_000 and s["cost"] > 0 and s["warn"]


def test_no_usage_yet_reads_none():
    from apps.agent_api import usage

    assert usage.month_summary() is None
