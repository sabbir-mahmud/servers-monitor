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

Token auth only, never the session cookie, so these are CSRF-exempt safely. A 401 from
check-in means the device was removed or paired again elsewhere: the app should forget
its token and show the pairing screen.
"""

import json

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.utils import client_ip
from apps.users import throttle

from . import services

MAX_BODY = 16 * 1024


def _error(status: int, message: str, **headers) -> JsonResponse:
    response = JsonResponse({"error": message}, status=status)
    for name, value in headers.items():
        response[name.replace("_", "-")] = value
    return response


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
    if not throttle.allow("device-pair", ip, 10, 600):
        return _error(429, "Too many tries. Wait 10 minutes.", Retry_After="600")
    data = _body(request)
    if data is None:
        return _error(400, "Send a JSON object.")
    try:
        device, token = services.pair(str(data.get("code", "")), data, client_ip(request))
    except services.PairingError as exc:
        return _error(exc.status, str(exc))
    return JsonResponse(
        {
            "token": token,
            "device": _device_json(device),
            "checkin_seconds": device.checkin_seconds,
            # Shown on the device before monitoring starts (store rules, and plain honesty).
            "disclosure": f"{device.account.name} will see this device's last online time, "
            "battery, storage, network and location.",
        },
        status=201,
    )


@csrf_exempt
@require_POST
def checkin(request):
    ip = client_ip(request) or "unknown"
    header = request.META.get("HTTP_AUTHORIZATION", "")
    device = services.authenticate(header[7:].strip()) if header.startswith("Bearer ") else None
    if device is None:
        if not throttle.allow("device-bad-token", ip, 30, 60):
            return _error(429, "Too many bad tokens. Wait a minute.", Retry_After="60")
        return _error(401, "This device isn't paired any more. Pair it again.",
                      WWW_Authenticate='Bearer error="invalid_token"')  # fmt: skip
    if not throttle.allow("device-checkin", str(device.pk), 30, 300):
        return _error(429, "Checking in too often.", Retry_After="300")
    data = _body(request)
    if data is None:
        return _error(400, "Send a JSON object.")
    services.checkin(device, data, client_ip(request))
    return JsonResponse(
        {
            "checkin_seconds": device.checkin_seconds,
            "server_time": timezone.now().isoformat(),
            "device": _device_json(device),
        }
    )
