"""Keep the agent builds the connect command installs in step with device-agent/.

The builds in device-agent/dist/ aren't committed, so after an agent change the dashboard
would go on serving the old program (which may no longer understand the install script).
When the dashboard runs next to the agent's source with Go installed, an install script
request starts a rebuild if any source file is newer than the builds. The script asks the
person for their password first, so by the time it downloads the agent the build is
usually done; agent downloads wait for a running build.

Builds go into a folder beside dist/ and replace it in one rename, so a download never
sees half a build. A file lock keeps the dashboard's processes from building at once. If a
build fails, the old builds keep being served and that source state isn't tried again.
"""

import fcntl
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

BUILD_TIMEOUT = 600  # seconds; six systems take about a minute
CHECK_EVERY = 5  # seconds between looks at the source files

_lock = threading.Lock()  # one build per process (the file lock covers the others)
_building: threading.Thread | None = None
_failed_for: float | None = None  # newest source time a build failed for
_checked: tuple[float, bool] = (0.0, False)  # when we last looked, and whether stale


def source_dir() -> Path:
    return Path(settings.DEVICE_AGENT_SOURCE)


def enabled() -> bool:
    """Only for the builds next to the source (not a DEVICE_AGENT_DIR of your own), and
    only where they can be built."""
    src = source_dir()
    return (
        settings.DEVICE_AGENT_AUTOBUILD
        and (src / "build.sh").is_file()
        and Path(settings.DEVICE_AGENT_DIR).resolve() == (src / "dist").resolve()
        and shutil.which("go") is not None
    )


def _newest_source() -> float:
    src = source_dir()
    newest = 0.0
    for path in [src / "go.mod", src / "go.sum", src / "VERSION", src / "build.sh"]:
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    for path in src.rglob("*.go"):
        if "dist" not in path.relative_to(src).parts:
            newest = max(newest, path.stat().st_mtime)
    return newest


def _built_at() -> float:
    sums = Path(settings.DEVICE_AGENT_DIR) / "SHA256SUMS"
    return sums.stat().st_mtime if sums.is_file() else 0.0


def stale() -> bool:
    global _checked
    now = time.monotonic()
    if now - _checked[0] < CHECK_EVERY:
        return _checked[1]
    newest = _newest_source()
    result = newest > _built_at() and newest != _failed_for
    _checked = (now, result)
    return result


def _build() -> None:
    global _failed_for, _checked
    src = source_dir()
    dist = Path(settings.DEVICE_AGENT_DIR)
    lock_path = src / ".build.lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)  # another process may be building: wait
        try:
            newest = _newest_source()
            if newest <= _built_at():
                return  # the other process built it
            fresh = src / f".dist-{os.getpid()}"
            shutil.rmtree(fresh, ignore_errors=True)
            logger.info("Agent builds are older than device-agent/: rebuilding")
            started = time.monotonic()
            result = subprocess.run(
                ["sh", str(src / "build.sh")],
                cwd=src,
                env={**os.environ, "DIST": str(fresh)},
                capture_output=True,
                text=True,
                timeout=BUILD_TIMEOUT,
            )
            if result.returncode != 0 or not (fresh / "SHA256SUMS").is_file():
                _failed_for = newest
                shutil.rmtree(fresh, ignore_errors=True)
                logger.error(
                    "Agent rebuild failed; still serving the old builds:\n%s",
                    (result.stderr or result.stdout)[-2000:],
                )
                return
            old = src / f".dist-old-{os.getpid()}"
            if dist.exists():
                dist.rename(old)  # open downloads keep reading the old files
            fresh.rename(dist)
            shutil.rmtree(old, ignore_errors=True)
            logger.info("Agent rebuilt in %.0f s", time.monotonic() - started)
        except (OSError, subprocess.SubprocessError) as exc:
            _failed_for = _newest_source()
            logger.error("Agent rebuild failed; still serving the old builds: %s", exc)
        finally:
            _checked = (0.0, False)
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def refresh_in_background() -> None:
    """Start a rebuild when the builds are stale; return at once."""
    global _building
    if not enabled() or not stale():
        return
    with _lock:
        if _building is not None and _building.is_alive():
            return
        _building = threading.Thread(target=_build, name="agent-build", daemon=True)
        _building.start()


def wait_until_fresh() -> None:
    """Before serving an agent file: wait for a running or needed rebuild, so the download
    and its checksum come from the same build."""
    refresh_in_background()
    thread = _building
    if thread is not None:
        thread.join(BUILD_TIMEOUT)
