"""The browser SSH terminal, against a real (in-process) SSH server."""

import base64
import json
import socket
import threading
import time

import paramiko
import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.operations import ssh
from apps.operations.models import SshHostKey

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _close_terminals():
    """Sessions live in process memory: don't let one test's count against the next."""
    yield
    for session in list(ssh._sessions.values()):
        ssh.close(session)
    ssh._sessions.clear()


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        ok = (username, password) == ("deploy", "s3cret")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_window_change_request(self, *args):
        return True


def _echo_shell(channel):
    channel.sendall(b"welcome\r\n$ ")
    while True:
        data = channel.recv(1024)
        if not data:
            break
        if b"exit" in data:
            channel.sendall(b"bye\r\n")
            break
        channel.sendall(b"you typed: " + data)
    channel.close()


@pytest.fixture
def sshd():
    key = paramiko.RSAKey.generate(1024)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    state = {"key": key}

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(state["key"])
            try:
                transport.start_server(server=_Server())
            except paramiko.SSHException, EOFError:
                continue
            channel = transport.accept(10)
            if channel is not None:
                threading.Thread(target=_echo_shell, args=(channel,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield {"port": port, "state": state}
    listener.close()


@pytest.fixture
def owner(client, admin_user, settings):
    settings.SSH_ALLOW_PRIVATE_TARGETS = True
    client.force_login(admin_user)
    return admin_user


def open_(client, port, **kw):
    body = {"host": "127.0.0.1", "port": port, "username": "deploy", "password": "s3cret", **kw}
    return client.post(reverse("operations:ssh_open"), json.dumps(body),
                       content_type="application/json")  # fmt: skip


def read_all(client, sid, until=b"", tries=20):
    out, offset = b"", 0
    for _ in range(tries):
        r = client.get(reverse("operations:ssh_read", args=[sid]), {"offset": offset}).json()
        out += base64.b64decode(r["data"])
        offset = r["offset"]
        if until in out or r["closed"]:
            return out, r
    return out, r


def test_password_session_types_reads_and_closes(client, owner, sshd):
    resp = open_(client, sshd["port"])
    assert resp.status_code == 200, resp.content
    sid = resp.json()["id"]
    out, _ = read_all(client, sid, b"$ ")
    assert b"welcome" in out
    client.post(reverse("operations:ssh_write", args=[sid]), json.dumps({"data": "ls\r"}),
                content_type="application/json")  # fmt: skip
    out, _ = read_all(client, sid, b"you typed: ls")
    assert b"you typed: ls" in out
    client.post(reverse("operations:ssh_write", args=[sid]),
                json.dumps({"cols": 100, "rows": 30}), content_type="application/json")  # fmt: skip
    client.post(reverse("operations:ssh_close", args=[sid]))
    _, r = read_all(client, sid)
    assert r["closed"]
    actions = list(AuditLog.objects.values_list("action", flat=True))
    assert "ssh.open" in actions and "ssh.close" in actions
    assert not any("s3cret" in json.dumps(a.data) for a in AuditLog.objects.all())


def test_host_key_is_saved_then_must_match(client, owner, sshd):
    assert open_(client, sshd["port"]).status_code == 200
    saved = SshHostKey.objects.get()
    assert saved.fingerprint.startswith("SHA256:")
    sshd["state"]["key"] = paramiko.RSAKey.generate(1024)  # "reinstalled"
    resp = open_(client, sshd["port"])
    assert resp.status_code == 400 and "host key" in resp.json()["error"]
    client.post(reverse("operations:ssh_forget_key", args=[saved.pk]))
    assert open_(client, sshd["port"]).status_code == 200


def test_wrong_password(client, owner, sshd):
    resp = open_(client, sshd["port"], password="nope")
    assert resp.status_code == 400 and "refused the password" in resp.json()["error"]
    assert AuditLog.objects.filter(action="ssh.failed").exists()


def test_private_addresses_need_the_setting(client, owner, sshd, settings):
    settings.SSH_ALLOW_PRIVATE_TARGETS = False
    resp = open_(client, sshd["port"])
    assert resp.status_code == 400 and "SSH_ALLOW_PRIVATE_TARGETS" in resp.json()["error"]


def test_metadata_address_is_never_allowed(client, owner):
    resp = open_(client, 22, host="169.254.169.254")
    assert resp.status_code == 400 and "blocked" in resp.json()["error"]


def test_someone_elses_session_is_invisible(client, owner, sshd, django_user_model, roles):
    from .conftest import add_member

    sid = open_(client, sshd["port"]).json()["id"]
    other = django_user_model.objects.create_user("other", "o@example.com", "pw-x-1234567")
    add_member(other, "admin")
    client.force_login(other)
    assert client.get(reverse("operations:ssh_read", args=[sid])).status_code == 404
    assert client.post(reverse("operations:ssh_write", args=[sid]), "{}",
                       content_type="application/json").status_code == 404  # fmt: skip


def test_viewers_and_operators_cannot_ssh(client, viewer, operator):
    for user in (viewer, operator):
        client.force_login(user)
        assert client.get(reverse("operations:ssh")).status_code == 403
        assert client.post(reverse("operations:ssh_open"), "{}",
                           content_type="application/json").status_code == 403  # fmt: skip


def test_page_prefills_from_the_server(client, owner, server):
    server.ssh_user, server.ssh_host = "ubuntu", "203.0.113.9"
    server.save()
    page = client.get(reverse("operations:ssh") + f"?server={server.pk}").content.decode()
    assert 'value="ubuntu"' in page and 'value="203.0.113.9"' in page


def test_idle_and_abandoned_sessions_close(client, owner, sshd):
    sid = open_(client, sshd["port"]).json()["id"]
    session = ssh.get(sid, owner)
    session.last_read = time.monotonic() - 120
    assert ssh.reap() == 1 and session.closed
    assert "page was closed" in session.reason


def test_needs_a_password_or_key(client, owner, sshd):
    resp = open_(client, sshd["port"], password="")
    assert "Type the password" in resp.json()["error"]


# --------------------------------------------------------------------------- saved logins


def test_saving_a_login_then_connecting_with_one_click(client, owner, sshd, server):
    from apps.operations.models import SshLogin

    resp = open_(client, sshd["port"], save=True, name="deploy box", server=str(server.pk))
    assert resp.status_code == 200, resp.content
    saved = resp.json()["saved"]
    login = SshLogin.objects.get(pk=saved["id"])
    assert (login.name, login.user, login.server, login.auth) == ("deploy box", owner, server,
                                                                   "password")  # fmt: skip
    assert "s3cret" not in login.secret and login.secret.startswith("enc1:")

    # Only the id goes over the wire: no address, user or password.
    resp = client.post(reverse("operations:ssh_open"), json.dumps({"login": saved["id"]}),
                       content_type="application/json")  # fmt: skip
    assert resp.status_code == 200, resp.content
    assert resp.json()["login"]["name"] == "deploy box"
    out, _ = read_all(client, resp.json()["id"], b"welcome")
    assert b"welcome" in out
    login.refresh_from_db()
    assert login.last_used_at is not None
    assert not any("s3cret" in json.dumps(a.data) for a in AuditLog.objects.all())
    assert AuditLog.objects.filter(action="ssh.login_saved").exists()


def test_a_login_is_saved_only_once_it_has_worked(client, owner, sshd):
    from apps.operations.models import SshLogin

    assert open_(client, sshd["port"], password="typo", save=True).status_code == 400
    assert not SshLogin.objects.exists()
    open_(client, sshd["port"], save=True)
    open_(client, sshd["port"], save=True, name="renamed")  # same user@host:port: replaced
    assert list(SshLogin.objects.values_list("name", flat=True)) == ["renamed"]


def test_saved_logins_are_private_to_their_owner(client, owner, sshd, django_user_model, roles):
    from apps.operations.models import SshLogin

    from .conftest import add_member

    saved = open_(client, sshd["port"], save=True, name="mine").json()["saved"]
    other = django_user_model.objects.create_user("other", "o@example.com", "pw-x-1234567")
    add_member(other, "admin")  # same account, same permission
    client.force_login(other)
    assert "mine" not in client.get(reverse("operations:ssh")).content.decode()
    resp = client.post(reverse("operations:ssh_open"), json.dumps({"login": saved["id"]}),
                       content_type="application/json")  # fmt: skip
    assert resp.status_code == 404
    delete = reverse("operations:ssh_login_delete", args=[saved["id"]])
    assert client.post(delete).status_code == 404
    assert SshLogin.objects.filter(pk=saved["id"]).exists()


def test_a_secret_copied_onto_another_row_does_not_open(client, owner, sshd):
    from apps.operations.models import SshLogin

    first = open_(client, sshd["port"], save=True).json()["saved"]
    SshLogin.objects.filter(pk=first["id"]).update(username="root")  # a second row…
    second = open_(client, sshd["port"], save=True).json()["saved"]
    stolen = SshLogin.objects.get(pk=first["id"]).secret
    SshLogin.objects.filter(pk=second["id"]).update(secret=stolen)  # …with the first's secret
    resp = client.post(reverse("operations:ssh_open"), json.dumps({"login": second["id"]}),
                       content_type="application/json")  # fmt: skip
    assert resp.status_code == 400 and "can't be opened" in resp.json()["error"]


def test_removing_a_saved_login(client, owner, sshd):
    from apps.operations.models import SshLogin

    saved = open_(client, sshd["port"], save=True).json()["saved"]
    delete = reverse("operations:ssh_login_delete", args=[saved["id"]])
    resp = client.post(delete, {"next": "https://evil.example/"})
    assert resp["Location"] == reverse("operations:ssh")  # never off-site
    assert not SshLogin.objects.exists()
    assert AuditLog.objects.filter(action="ssh.login_removed").exists()


def test_server_page_offers_saved_logins_and_auto_connects(client, owner, sshd, server):
    saved = open_(client, sshd["port"], save=True, name="as deploy",
                  server=str(server.pk)).json()["saved"]  # fmt: skip
    detail = client.get(reverse("fleet:server_detail", args=[server.pk])).content.decode()
    assert "as deploy" in detail and f"login={saved['id']}" in detail
    page = client.get(reverse("operations:ssh"),
                      {"server": server.pk, "login": saved["id"]}).content.decode()  # fmt: skip
    assert f'data-auto="{saved["id"]}"' in page
    page = client.get(reverse("operations:ssh"), {"login": "not-a-uuid"})
    assert page.status_code == 200 and "data-auto" not in page.content.decode()
