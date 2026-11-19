"""The browser terminal's page and its small JSON API (see ssh.py)."""

import json

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin
from apps.fleet.models import Server
from apps.tenants.scoping import get_scoped_or_404, scoped

from . import ssh
from .models import SshHostKey

PERM = "operations.run_shell"  # the same people who may run shell commands: Owner, Admin


class SshPageView(StaffViewMixin, TemplateView):
    permission_required = PERM
    template_name = "operations/ssh.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = None
        if pk := self.request.GET.get("server"):
            server = get_scoped_or_404(self.request, Server, pk=pk)
        ctx.update(
            server=server,
            host=(server.ssh_host or server.remote_addr or "") if server else "",
            port=server.ssh_port if server else 22,
            username=server.ssh_user if server else "",
            servers=scoped(self.request, Server).order_by("name"),
            host_keys=scoped(self.request, SshHostKey).order_by("host", "port"),
            idle_minutes=settings.SSH_IDLE_MINUTES,
            insecure=not self.request.is_secure()
            and self.request.META.get("REMOTE_ADDR") not in ("127.0.0.1", "::1"),
        )
        return ctx


def _json(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _int(value, default, low, high) -> int:
    try:
        return max(low, min(high, int(value)))
    except TypeError, ValueError:
        return default


class SshOpenView(StaffViewMixin, View):
    permission_required = PERM

    def post(self, request):
        data = _json(request)
        host = str(data.get("host") or "").strip()
        username = str(data.get("username") or "").strip()
        port = _int(data.get("port"), 22, 1, 65535)
        if not host or len(host) > 255 or any(c.isspace() for c in host):
            return JsonResponse(
                {"error": "Type the server's address (IP or hostname)."}, status=400
            )
        if not username or len(username) > 64:
            return JsonResponse({"error": "Type the user to sign in as."}, status=400)
        server = None
        if data.get("server"):
            server = scoped(request, Server).filter(pk=data["server"]).first()
        try:
            session = ssh.open_session(
                user=request.user, account=request.account, host=host, port=port,
                username=username, password=str(data.get("password") or ""),
                private_key=str(data.get("private_key") or ""),
                passphrase=str(data.get("passphrase") or ""),
                cols=_int(data.get("cols"), 120, 10, 500), rows=_int(data.get("rows"), 32, 3, 200),
            )  # fmt: skip
        except ssh.SshError as exc:
            record(request, "ssh.failed", target=f"{username}@{host}:{port}", server=server,
                   error=str(exc)[:200])  # fmt: skip
            return JsonResponse({"error": str(exc)}, status=400)
        user, account, label = request.user, request.account, session.label
        session.on_close = lambda reason: record(user, "ssh.close", target=label,
                                                 account=account, server=server,
                                                 seconds=ssh.duration(session), reason=reason)  # fmt: skip
        record(request, "ssh.open", target=session.label, server=server)
        if server is not None:  # fill these in next time
            Server.objects.filter(pk=server.pk).update(ssh_host=host, ssh_port=port,
                                                       ssh_user=username)  # fmt: skip
        return JsonResponse({"id": session.id, "label": session.label})


class _SessionView(StaffViewMixin, View):
    permission_required = PERM

    def session(self, sid):
        session = ssh.get(sid, self.request.user)
        if session is None:
            raise Http404
        return session


class SshReadView(_SessionView):
    def get(self, request, sid):
        offset = _int(request.GET.get("offset"), 0, 0, 2**62)
        return JsonResponse(ssh.read(self.session(sid), offset))


class SshWriteView(_SessionView):
    def post(self, request, sid):
        data = _json(request)
        session = self.session(sid)
        if "cols" in data:
            ssh.resize(session, _int(data.get("cols"), 120, 10, 500),
                       _int(data.get("rows"), 32, 3, 200))  # fmt: skip
        text = data.get("data")
        if isinstance(text, str) and text:
            ssh.write(session, text[:65536])
        return JsonResponse({"closed": session.closed})


class SshCloseView(_SessionView):
    def post(self, request, sid):
        ssh.close(self.session(sid))
        return JsonResponse({"closed": True})


class SshForgetKeyView(StaffViewMixin, View):
    """After a deliberate reinstall: drop the saved host key so the new one is accepted."""

    permission_required = PERM

    def post(self, request, pk):
        key = get_scoped_or_404(request, SshHostKey, pk=pk)
        record(request, "ssh.forget_host_key", target=f"{key.host}:{key.port}",
               fingerprint=key.fingerprint)  # fmt: skip
        key.delete()
        return redirect(request.POST.get("next") or reverse("operations:ssh"))
