"""Server tasks: routine maintenance a server's agent runs as root through `smctl task`.

The dashboard only ever sends a task's name. Each server opts in per task
(`sudo smctl tasks on upgrade reboot`), and the agent and smctl both re-check that, so the
dashboard can't widen what a server allows. Which commands run depends on the server's
package manager (apt, dnf, yum, zypper, apk or pacman); see agent/smctl.py.
"""

from dataclasses import dataclass

ACTION = "_task"


@dataclass(frozen=True)
class Task:
    key: str
    label: str
    description: str
    icon: str
    timeout: int  # seconds, as the agent allows (agent/server_monitor_agent.py TASKS)
    admin: bool  # changes the server: Owner/Admin only. Otherwise anyone who runs jobs.
    confirm: str = ""  # asked before running from the server page


TASKS = {
    t.key: t
    for t in (
        Task("updates", "Check for updates", "Refresh package lists and list what can be "
             "upgraded. Changes nothing.", "search", 900, admin=False),
        Task("upgrade", "Upgrade packages", "Refresh package lists, then upgrade every "
             "package. Changed config files are kept.", "download", 3600, admin=True,
             confirm="Upgrade all packages on {server}? Services may restart while it runs."),
        Task("cleanup", "Clean up", "Remove packages nothing needs, clear the package cache "
             "and trim journal logs older than 14 days.", "trash", 900, admin=True,
             confirm="Clean up {server}? Unused packages are removed."),
        Task("reboot", "Reboot", "Restart the server one minute from now. It goes offline "
             "for a while.", "refresh", 60, admin=True,
             confirm="Reboot {server}? It restarts in one minute and is offline until it's back."),
    )
}  # fmt: skip

PACKAGE_MANAGERS = {
    "apt": "apt (Debian, Ubuntu)",
    "dnf": "dnf (Fedora, RHEL)",
    "yum": "yum (CentOS, older RHEL)",
    "zypper": "zypper (openSUSE, SLES)",
    "apk": "apk (Alpine)",
    "pacman": "pacman (Arch)",
}


def label(key: str) -> str:
    task = TASKS.get(key)
    return task.label if task else key


def can_run(user, key: str) -> bool:
    task = TASKS.get(key)
    if task is None or user is None:
        return False
    return user.has_perm("fleet.change_server" if task.admin else "operations.add_job")
