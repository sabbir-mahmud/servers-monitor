"""Checksums of the agent files this dashboard was deployed with.

Install and upgrade commands carry them, and the installer refuses anything from GitHub
that doesn't match. So servers only ever run the agent code you deployed, even if the
repository (or the account behind it) is compromised later.
"""

import hashlib
from functools import cache

from django.conf import settings

FILES = {
    "install": "install.sh",
    "agent": "server_monitor_agent.py",
    "smctl": "smctl.py",
    "unit": "server-monitor-agent.service",
    "upath": "server-monitor-upgrade.path",
    "uservice": "server-monitor-upgrade.service",
}
# Files checked by the installer itself (install.sh is checked before it runs).
INSTALLED = ("agent", "smctl", "unit", "upath", "uservice")


class AgentFilesMissing(Exception):
    pass


@cache
def _sums_for(signature: tuple) -> dict[str, str]:
    base = settings.AGENT_DIST_DIR
    out = {}
    for key, name in FILES.items():
        try:
            out[key] = hashlib.sha256((base / name).read_bytes()).hexdigest()
        except OSError as exc:
            raise AgentFilesMissing(f"agent file {base / name} is missing: {exc}") from exc
    return out


def sums() -> dict[str, str]:
    base = settings.AGENT_DIST_DIR
    signature = tuple(
        (n, (base / n).stat().st_mtime_ns if (base / n).exists() else 0) for n in FILES.values()
    )
    return _sums_for(signature)


def sums_arg() -> str:
    s = sums()
    return ",".join(f"{k}={s[k]}" for k in INSTALLED)


def install_command(ref: str | None = None) -> str:
    """Download install.sh, check its SHA-256, run it with the other files' checksums.
    The connection string is NOT in it: the installer asks for it at a hidden prompt."""
    ref = ref or settings.AGENT_REF
    url = f"https://raw.githubusercontent.com/{settings.AGENT_REPO}/{ref}/agent/install.sh"
    return (
        f"curl -fsSL {url} -o sm-install.sh \\\n"
        f'  && echo "{sums()["install"]}  sm-install.sh" | sha256sum -c --quiet - \\\n'
        f"  && sudo SM_REF={ref} SM_SUMS={sums_arg()} bash sm-install.sh"
    )


def upgrade_command() -> str:
    s = sums()
    return (
        f"sudo smctl upgrade --ref {settings.AGENT_REF} --sums install={s['install']},{sums_arg()}"
    )


def agent_version() -> str:
    """The agent version this dashboard ships (what "outdated" is measured against)."""
    import re

    try:
        text = (settings.AGENT_DIST_DIR / FILES["agent"]).read_text()
    except OSError:
        return ""
    found = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return found.group(1) if found else ""


RELEASE_TAG = r"^v\d{1,4}\.\d{1,4}\.\d{1,4}$"


def upgrade_args() -> dict | None:
    """Arguments of the built-in `_upgrade` command, or None when remote upgrades can't be
    offered: they only ever install a release tag (pin AGENT_REF to one)."""
    import re

    if not re.match(RELEASE_TAG, settings.AGENT_REF):
        return None
    try:
        return {"ref": settings.AGENT_REF, "sums": dict(sums())}
    except AgentFilesMissing:
        return None
