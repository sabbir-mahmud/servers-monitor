"""Local database snapshots (daily, by the relay process) and restore.

Uses SQLite's online backup API, so a snapshot is consistent even while the dashboard and
relay are writing. For continuous off-site copies, run the optional Litestream service
(docker compose --profile backup); see docs/deployment.md.
"""

import logging
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import SystemState

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=36)
PREFIX = "db-"


def backup_dir() -> Path:
    return (
        Path(settings.BACKUP_DIR)
        if settings.BACKUP_DIR
        else settings.SQLITE_PATH.parent / "backups"
    )


def snapshots() -> list[Path]:
    folder = backup_dir()
    return sorted(folder.glob(f"{PREFIX}*.sqlite3")) if folder.exists() else []


def take_snapshot() -> dict:
    """Write one snapshot, rotate old ones, record the result for the System page."""
    folder = backup_dir()
    now = timezone.now()
    target = folder / f"{PREFIX}{now:%Y%m%d-%H%M%S}.sqlite3"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(settings.SQLITE_PATH)
        dst = sqlite3.connect(target)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
        for old in snapshots()[: -settings.BACKUP_KEEP]:
            old.unlink(missing_ok=True)
        state = {"ok": True, "at": now.isoformat(), "path": str(target),
                 "size": target.stat().st_size, "count": len(snapshots())}  # fmt: skip
    except (OSError, sqlite3.Error) as exc:
        logger.error("Database snapshot failed: %s", exc)
        target.unlink(missing_ok=True)
        state = {"ok": False, "at": now.isoformat(), "error": str(exc)[:200]}
    SystemState.put("backup", state)
    return state


def snapshot_due(now=None) -> bool:
    last = SystemState.get("backup")
    if not last or not last.get("ok"):
        return True
    from datetime import datetime

    return (now or timezone.now()) - datetime.fromisoformat(last["at"]) >= timedelta(hours=24)


def is_stale(state, now=None) -> bool:
    from datetime import datetime

    if not state or not state.get("ok"):
        return True
    return (now or timezone.now()) - datetime.fromisoformat(state["at"]) > STALE_AFTER


def check_integrity(path: Path) -> str:
    """'ok' or SQLite's description of what's wrong."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("SELECT count(*) FROM django_migrations").fetchone()
        finally:
            conn.close()
        return result
    except sqlite3.Error as exc:
        return str(exc)


def restore(path: Path) -> Path:
    """Replace the live database with `path`. The caller makes sure nothing is running.
    Returns where the database that was replaced has been kept."""
    live = Path(settings.SQLITE_PATH)
    kept = live.with_name(f"{live.stem}.before-restore-{timezone.now():%Y%m%d-%H%M%S}.sqlite3")
    if live.exists():
        shutil.copy2(live, kept)
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    shutil.copy2(path, live)
    return kept
