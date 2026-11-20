"""The browser terminal's page and its small JSON API (see ssh.py)."""

import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Case, IntegerField, Value, When
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin
from apps.fleet.models import Server
from apps.tenants.scoping import get_scoped_or_404, scoped

from . import ssh, ssh_logins
from .models import SshHostKey, SshLogin

PERM = "operations.run_shell"  # the same people who may run shell commands: Owner, Admin


class SshPageView(StaffViewMixin, TemplateView):
    permission_required = PERM
    template_name = "operations/ssh.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = None
        if pk := self.request.GET.get("server"):
            server = get_scoped_or_404(self.request, Server, pk=pk)
        logins = ssh_logins.mine(self.request).select_related("server")
        if server is not None:  # this server's logins first
            logins = logins.annotate(
                here=Case(
                    When(server=server, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )  # fmt: skip
            ).order_by("here", "name")
        auto = _login(self.request, self.request.GET.get("login"))
        ctx.update(
            logins=logins,
            auto_login=auto,
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


def _login(request, pk) -> SshLogin | None:
    """One of the caller's own saved logins, or None (someone else's looks like none)."""
    if not pk:
        return None
    try:
        return ssh_logins.mine(request).select_related("server").filter(pk=pk).first()
    except ValidationError:  # not a UUID
        return None


def _next(request, default: str) -> str:
    url = request.POST.get("next") or ""
    ok = url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                         require_https=request.is_secure())  # fmt: skip
    return url if ok else default


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
    """Open a terminal: with typed-in credentials (optionally saving them once they've
    worked), or with one of the caller's saved logins."""

    permission_required = PERM

    def post(self, request):
        data = _json(request)
        login = None
        if data.get("login"):
            login = _login(request, str(data["login"]))
            if login is None:
                return JsonResponse({"error": "That saved login is gone."}, status=404)
            host, port, username, server = login.host, login.port, login.username, login.server
            try:
                secrets_ = ssh_logins.unlock(login)
            except ssh_logins.LoginError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
        else:
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
                try:
                    server = scoped(request, Server).filter(pk=data["server"]).first()
                except ValidationError:
                    server = None
            secrets_ = {
                k: str(data.get(k) or "") for k in ("password", "private_key", "passphrase")
            }
        try:
            session = ssh.open_session(
                user=request.user, account=request.account, host=host, port=port,
                username=username, cols=_int(data.get("cols"), 120, 10, 500),
                rows=_int(data.get("rows"), 32, 3, 200), **secrets_,
            )  # fmt: skip
        except ssh.SshError as exc:
            message = str(exc)
            if login is not None and "refused" in message:
                message += (" The saved login may be out of date: connect with the new "
                            "password or key and save it again to replace it.")  # fmt: skip
            record(request, "ssh.failed", target=f"{username}@{host}:{port}", server=server,
                   error=str(exc)[:200], **({"login": login.name} if login else {}))  # fmt: skip
            return JsonResponse({"error": message}, status=400)
        finally:
            secrets_ = None  # don't keep the plaintext around longer than the connection
        user, account, label = request.user, request.account, session.label
        session.on_close = lambda reason: record(user, "ssh.close", target=label,
                                                 account=account, server=server,
                                                 seconds=ssh.duration(session), reason=reason)  # fmt: skip
        record(request, "ssh.open", target=session.label, server=server,
               **({"login": login.name} if login else {}))  # fmt: skip
        reply = {"id": session.id, "label": session.label, "login": None, "saved": None}
        if login is not None:
            SshLogin.objects.filter(pk=login.pk).update(last_used_at=timezone.now())
            reply["login"] = {"id": str(login.pk), "name": login.name}
        elif data.get("save"):
            try:
                saved = ssh_logins.save(
                    request=request, server=server, host=host, port=port, username=username,
                    name=str(data.get("name") or ""), key_fingerprint=session.key_fingerprint,
                    password=str(data.get("password") or ""),
                    private_key=str(data.get("private_key") or ""),
                    passphrase=str(data.get("passphrase") or ""),
                )  # fmt: skip
            except ssh_logins.LoginError as exc:
                reply["save_error"] = str(exc)
            else:
                record(request, "ssh.login_saved", target=saved.name, server=server,
                       to=saved.target, auth=saved.auth)  # fmt: skip
                reply["login"] = reply["saved"] = {"id": str(saved.pk), "name": saved.name}
        if server is not None:  # fill these in next time
            Server.objects.filter(pk=server.pk).update(ssh_host=host, ssh_port=port,
                                                       ssh_user=username)  # fmt: skip
        return JsonResponse(reply)


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
        return redirect(_next(request, reverse("operations:ssh")))


class SshLoginDeleteView(StaffViewMixin, View):
    """Remove one of your saved logins (its encrypted secret goes with it)."""

    permission_required = PERM

    def post(self, request, pk):
        login = _login(request, str(pk))
        if login is None:
            raise Http404
        record(request, "ssh.login_removed", target=login.name, server=login.server,
               to=login.target)  # fmt: skip
        login.delete()
        return redirect(_next(request, reverse("operations:ssh")))
