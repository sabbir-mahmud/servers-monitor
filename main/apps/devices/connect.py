"""Connecting a computer: one link to open on it.

The dashboard shows a link like https://monitor.example.com/d/K7PQ-3MXA. Opened on the
computer, it says how to get the NexGuard desktop app, and has an "Open in NexGuard"
button (a nexguard://pair link, the same one the phone app's QR code holds) that fills in
the dashboard address and code. The app shows who will see what and pairs only on a yes.

Accounts that allow it (Account settings > Pairing) get `?via=command` on the link: the
page then also offers the one line to paste into PowerShell or Terminal. It downloads a
short script from this dashboard with the code filled in, which shows the same disclosure,
pairs on a yes and installs the agent. Pairing refuses that route for accounts that need
the app, so the query string is only about what the page shows.

These pages need no sign-in and never look the code up: a well-formed code is all they
check, so they can't be used to test whether a code is valid. Pairing itself (POST
/device/v1/pair) is where codes are checked, and it is rate-limited.
"""

import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from . import pairing

CODE_RE = re.compile(f"^[{pairing.ALPHABET}]{{{pairing.CODE_LENGTH}}}$")
# What may be pasted between single quotes in a shell or PowerShell script.
SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]/_~%]+$")

# How each system's user opens the place to paste the command, in their words.
SYSTEMS = {
    "windows": {
        "label": "Windows",
        "app": "PowerShell",
        "open": "Press the Windows key, type PowerShell, then press Enter.",
        "paste": "Press Ctrl V to paste (or right-click in the window), then press Enter.",
    },
    "macos": {
        "label": "Mac",
        "app": "Terminal",
        "open": "Press ⌘ Space, type Terminal, then press Return.",
        "paste": "Press ⌘ V to paste, then press Return.",
    },
    "linux": {
        "label": "Linux",
        "app": "Terminal",
        "open": "Press Ctrl Alt T (or open Terminal from your apps).",
        "paste": "Press Ctrl Shift V to paste, then press Enter.",
    },
}


def clean_code(raw: str) -> str:
    code = pairing.normalize_code(raw)
    if not CODE_RE.match(code):
        raise Http404("Not a pairing code.")
    return code


def server_url(request) -> str:
    url = (settings.DASHBOARD_URL or request.build_absolute_uri("/")).rstrip("/")
    if not SAFE_URL_RE.match(url):
        # Never paste an unexpected character into a script someone will run.
        raise Http404("Set DASHBOARD_URL to this dashboard's address.")
    return url


def commands(server: str, code: str) -> dict:
    """The one line to paste, per system."""
    base = f"{server}/d/{code}"
    return {
        "windows": f"irm {base}/install.ps1 | iex",
        "macos": f"curl -fsSL {base}/install.sh | sh",
        "linux": f"curl -fsSL {base}/install.sh | sh",
    }


def link(server: str, code: str, *, command: bool = False) -> str:
    return f"{server}/d/{code[:4]}-{code[4:]}" + ("?via=command" if command else "")


def app_link(server: str, code: str) -> str:
    """Opens the desktop app (or the phone app) at its pair screen, filled in."""
    return "nexguard://pair?" + urlencode({"server": server, "code": code})


def connect_context(server: str, code: str, *, command: bool = False) -> dict:
    code = pairing.normalize_code(code)
    cmds = commands(server, code)
    return {
        "connect_link": link(server, code, command=command),
        "allow_command": command,
        "app_link": app_link(server, code),
        "app_url": settings.DESKTOP_APP_URL,
        "display_code": f"{code[:4]}-{code[4:]}",
        "server_url": server,
        "systems": [{"key": key, "command": cmds[key], **info} for key, info in SYSTEMS.items()],
        "uninstall": {
            "windows": f"irm {server}/d/uninstall.ps1 | iex",
            "unix": f"curl -fsSL {server}/d/uninstall.sh | sh",
        },
        "is_plain_http": urlsplit(server).scheme == "http",
    }


def _private(response: HttpResponse) -> HttpResponse:
    # The code is in the URL: keep it out of caches, search engines and Referer headers.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def page(request, code):
    code = clean_code(code)
    ctx = connect_context(server_url(request), code, command=request.GET.get("via") == "command")
    ctx["AUTH_PAGE"] = True  # no sidebar, even for a signed-in parent testing the link
    return _private(render(request, "devices/connect.html", ctx))


def install_script(request, code, kind):
    code = clean_code(code)
    template = {"sh": "devices/install/install.sh", "ps1": "devices/install/install.ps1"}[kind]
    body = render_to_string(
        template, {"server": server_url(request), "code": code, "site_name": settings.SITE_NAME}
    )
    # Windows PowerShell 5.1 reads scripts as the system code page; CRLF and pure ASCII
    # (the templates are) keep it happy. Shells want LF.
    if kind == "ps1":
        body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    return _private(response)


# The agent programs build.sh writes, and their checksums. Nothing else in that folder.
AGENT_FILE_RE = re.compile(
    r"^(nexguard-(darwin|linux)-(arm64|amd64)|nexguard-windows-(arm64|amd64)\.exe|SHA256SUMS)$"
)


def agent_file(request, name):
    """The agent for the connect command to install. Public like the scripts: it holds no
    secret, and the pairing code is what lets it in."""
    if not AGENT_FILE_RE.match(name):
        raise Http404("Not an agent file.")
    path = Path(settings.DEVICE_AGENT_DIR) / name
    if not path.is_file():
        raise Http404("This dashboard has no agent build for that system.")
    text = name == "SHA256SUMS"
    response = FileResponse(
        path.open("rb"),
        content_type="text/plain; charset=utf-8" if text else "application/octet-stream",
    )
    response["Cache-Control"] = "no-cache"  # a new build replaces it under the same name
    return response


def uninstall_script(request, kind):
    template = {"sh": "devices/install/uninstall.sh", "ps1": "devices/install/uninstall.ps1"}[kind]
    body = render_to_string(template, {"site_name": settings.SITE_NAME})
    if kind == "ps1":
        body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    return _private(HttpResponse(body, content_type="text/plain; charset=utf-8"))
