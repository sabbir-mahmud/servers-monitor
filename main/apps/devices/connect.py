"""Connecting a computer without installing anything first: one link, one line to paste.

The dashboard shows a link like https://monitor.example.com/d/K7PQ-3MXA. Opened on the
computer, it shows the one command for that computer's system (Windows PowerShell, or
Terminal on a Mac or Linux) with a Copy button. The command downloads a short script from
this dashboard with the code already filled in. The script shows who will see what, pairs
only if the person says yes, and installs a check-in that runs as that user every few
minutes (no administrator rights, nothing to keep open).

These pages need no sign-in and never look the code up: a well-formed code is all they
check, so they can't be used to test whether a code is valid. Pairing itself (POST
/device/v1/pair) is where codes are checked, and it is rate-limited.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

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


def link(server: str, code: str) -> str:
    return f"{server}/d/{code[:4]}-{code[4:]}"


def connect_context(server: str, code: str) -> dict:
    code = pairing.normalize_code(code)
    cmds = commands(server, code)
    return {
        "connect_link": link(server, code),
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
    ctx = connect_context(server_url(request), code)
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


def android_apk() -> Path | None:
    """The newest Android build of the phone app in PHONE_APP_DIR, or None."""
    folder = Path(settings.PHONE_APP_DIR)
    if not folder.is_dir():
        return None
    found = [p for p in folder.glob("*.apk") if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def android_app(request):
    """The NexGuard phone app for Android, straight to the phone. Public, like the agent: it
    holds no secret, and pairing needs a code."""
    path = android_apk()
    if path is None:
        raise Http404("This dashboard has no Android build of NexGuard yet.")
    response = FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename="NexGuard.apk",
        content_type="application/vnd.android.package-archive",
    )
    response["Cache-Control"] = "no-cache"  # a new build replaces it under the same link
    return response


def uninstall_script(request, kind):
    template = {"sh": "devices/install/uninstall.sh", "ps1": "devices/install/uninstall.ps1"}[kind]
    body = render_to_string(template, {"site_name": settings.SITE_NAME})
    if kind == "ps1":
        body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    return _private(HttpResponse(body, content_type="text/plain; charset=utf-8"))
