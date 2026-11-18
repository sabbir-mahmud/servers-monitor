"""The HTTP API the device apps talk to: /device/v1/.

    POST /device/v1/pair     {"code": "K7PQ-3MXA", "platform": "android", "model": …}
                             -> 201 {"token": "ngd_…", "device": {…}, "checkin_seconds": 900}
    POST /device/v1/checkin  Authorization: Bearer ngd_…
                             {"battery": {"percent": 81, "charging": false},
                              "storage": {"free": …, "total": …},
                              "network": {"type": "wifi", "name": "Home"},
                              "location": {"lat": 23.81, "lon": 90.41, "accuracy": 12,
                                           "at": "2026-11-17T08:00:00Z", "source": "gps"},
                              "os_version": "Android 16", "app_version": "1.0.0"}
                             -> 200 {"checkin_seconds": 900, "server_time": "…"}

    POST /device/v1/pair     {"code": "K7PQ-3MXA", "preview": true, "platform": …}
                             -> 200 {"disclosure": "…", "device": {…}}  (the code stays valid)

Clients that send `Accept: text/plain` (the shell installer) get the same fields as
`key=value` lines instead of JSON, nested objects flattened with `_` (`device_name=…`).

Token auth only, never the session cookie, so these are CSRF-exempt safely. A 401 from
check-in means the device was removed or paired again elsewhere: the app should forget
its token and show the pairing screen.
"""

import json

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.utils import client_ip
from apps.users import throttle

from . import services
from .models import COMPUTERS

MAX_BODY = 16 * 1024


def _wants_text(request) -> bool:
    return "text/plain" in request.META.get("HTTP_ACCEPT", "")


def _lines(data: dict, prefix: str = "") -> list[str]:
    """{"device": {"name": "X"}} -> ["device_name=X"]: one value per line, newlines folded
    to spaces, so a POSIX shell can read it with `sed` and nothing else."""
    out = []
    for key, value in data.items():
        if isinstance(value, dict):
            out += _lines(value, f"{prefix}{key}_")
        elif value is not None:
            text = str(value).lower() if isinstance(value, bool) else str(value)
            out.append(f"{prefix}{key}=" + " ".join(text.split()))
    return out


def _respond(request, data: dict, status: int = 200, **headers) -> HttpResponse:
    if _wants_text(request):
        body = "\n".join(_lines(data)) + "\n"
        response = HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")
    else:
        response = JsonResponse(data, status=status)
    for name, value in headers.items():
        response[name.replace("_", "-")] = value
    return response


def _error(request, status: int, message: str, **headers) -> HttpResponse:
    return _respond(request, {"error": message}, status, **headers)


def _body(request) -> dict | None:
    if len(request.body) > MAX_BODY:
        return None
    try:
        data = json.loads(request.body or b"{}")
    except ValueError, UnicodeDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _device_json(device) -> dict:
    return {
        "id": str(device.pk),
        "name": device.name,
        "platform": device.platform,
        "account": device.account.name,
        "person": device.person.name if device.person else None,
    }


@csrf_exempt
@require_POST
def pair(request):
    ip = client_ip(request) or "unknown"
    # 10 tries per 10 minutes per IP: plenty for typos, useless for guessing 40-bit codes.
    # Previews count too: they answer "is this code valid?" just like a pairing does.
    if not throttle.allow("device-pair", ip, 10, 600):
        return _error(request, 429, "Too many tries. Wait 10 minutes.", Retry_After="600")
    data = _body(request)
    if data is None:
        return _error(request, 400, "Send a JSON object.")
    code = str(data.get("code", ""))
    try:
        if data.get("preview") is True:
            device = services.preview(code, data)
            return _respond(
                request, {"disclosure": _disclosure(device), "device": _device_json(device)}
            )
        device, token = services.pair(code, data, client_ip(request))
    except services.PairingError as exc:
        return _error(request, exc.status, str(exc))
    return _respond(
        request,
        {
            "token": token,
            "device": _device_json(device),
            "checkin_seconds": device.checkin_seconds,
            "disclosure": _disclosure(device),
        },
        status=201,
    )


def _disclosure(device) -> str:
    """Shown on the device before monitoring starts (store rules, and plain honesty).
    It names exactly what that kind of device sends: computers send no location."""
    what = (
        "CPU, memory, GPU, battery and storage use, network (Wi-Fi name), and system version"
        if device.platform in COMPUTERS
        else "battery, storage, network and location"
    )
    return f"{device.account.name} will see this device's last online time, {what}."


@csrf_exempt
@require_POST
def checkin(request):
    ip = client_ip(request) or "unknown"
    header = request.META.get("HTTP_AUTHORIZATION", "")
    device = services.authenticate(header[7:].strip()) if header.startswith("Bearer ") else None
    if device is None:
        if not throttle.allow("device-bad-token", ip, 30, 60):
            return _error(request, 429, "Too many bad tokens. Wait a minute.", Retry_After="60")
        return _error(request, 401, "This device isn't paired any more. Pair it again.",
                      WWW_Authenticate='Bearer error="invalid_token"')  # fmt: skip
    if not throttle.allow("device-checkin", str(device.pk), 30, 300):
        return _error(request, 429, "Checking in too often.", Retry_After="300")
    data = _body(request)
    if data is None:
        return _error(request, 400, "Send a JSON object.")
    services.checkin(device, data, client_ip(request))
    return _respond(
        request,
        {
            "checkin_seconds": device.checkin_seconds,
            "server_time": timezone.now().isoformat(),
            "device": _device_json(device),
        },
    )
