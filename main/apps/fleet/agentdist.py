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
}


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
    return ",".join(f"{k}={s[k]}" for k in ("agent", "smctl", "unit"))


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
