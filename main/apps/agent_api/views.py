"""HTTP API the child agents call. Agents only ever dial out to main: no inbound port is
opened on any monitored server, which keeps them behind NAT/firewalls and shrinks the
attack surface to one authenticated HTTPS endpoint.

Bearer-token auth, no cookies — so CSRF does not apply and the views are csrf_exempt."""

import json
import logging
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.utils import client_ip
from apps.fleet.services import ingest_report, server_for_token
from apps.operations.services import claim_commands, report_progress

from .schema import PayloadError, clean_report, clean_result

logger = logging.getLogger(__name__)


def agent_auth(view):
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        server = server_for_token(token.strip()) if scheme.lower() == "bearer" else None
        if server is None:
            return JsonResponse({"error": "invalid token"}, status=401)
        request.server = server
        return view(request, *args, **kwargs)

    return wrapper


def _json_body(request):
    try:
        return json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise PayloadError("body is not valid JSON") from exc


def _schedule(server) -> dict:
    return {
        "server": server.name,
        "report_interval": server.report_interval,
        "command_poll_interval": server.command_poll_interval,
        "enabled": server.is_enabled,
    }


@agent_auth
@require_POST
def heartbeat(request):
    server = request.server
    try:
        report = clean_report(_json_body(request))
    except PayloadError as exc:
        logger.warning("Bad heartbeat from %s: %s", server.name, exc)
        return JsonResponse({"error": str(exc)}, status=400)
    ingest_report(server, report, client_ip(request))
    return JsonResponse({**_schedule(server), "commands": claim_commands(server)})


@agent_auth
@require_GET
def poll_commands(request):
    server = request.server
    return JsonResponse({**_schedule(server), "commands": claim_commands(server)})


@agent_auth
@require_POST
def command_result(request, pk):
    try:
        result = clean_result(_json_body(request))
    except PayloadError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    command = report_progress(
        request.server, pk, result["status"], result["output"], result["exit_code"]
    )
    if command is None:
        return JsonResponse({"error": "not found"}, status=404)
    # `cancel: true` tells a streaming agent to stop work nobody is waiting for any more.
    return JsonResponse({"status": command.status, "cancel": command.status == "lost"})
