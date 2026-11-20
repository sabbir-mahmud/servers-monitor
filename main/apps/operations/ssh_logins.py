"""Saved SSH logins: the only code that encrypts or decrypts their secrets.

The secret is a small JSON document encrypted with apps/core/crypto (Fernet, key derived
from SECRET_KEY). It also names the login and the user it belongs to, and `unlock` checks
both: a ciphertext copied onto someone else's row (by anyone with database access) won't
open. Decrypted secrets live only for the length of the request that opens a connection.
"""

import json

from django.db import transaction
from django.utils import timezone

from apps.core import crypto

from .models import SshAuth, SshLogin

MAX_PER_USER = 50


class LoginError(Exception):
    """Something to show the person."""


def _seal(login: SshLogin, password: str, private_key: str, passphrase: str) -> str:
    return crypto.encrypt(json.dumps({
        "v": 1, "login": str(login.pk), "user": login.user_id,
        "password": password, "private_key": private_key, "passphrase": passphrase,
    }))  # fmt: skip


def unlock(login: SshLogin) -> dict:
    """{password, private_key, passphrase} for opening a connection right now."""
    try:
        data = json.loads(crypto.decrypt(login.secret) or "{}")
    except ValueError:
        data = {}
    if data.get("login") != str(login.pk) or data.get("user") != login.user_id:
        raise LoginError(
            f"The saved login “{login.name}” can't be opened any more (the dashboard's "
            "secret key changed, or it was tampered with). Remove it and save it again."
        )
    return {k: str(data.get(k) or "") for k in ("password", "private_key", "passphrase")}


def save(*, request, server, host: str, port: int, username: str, name: str,
         password: str, private_key: str, passphrase: str,
         key_fingerprint: str = "") -> SshLogin:  # fmt: skip
    """Create, or replace, the caller's login for username@host:port. Call it only after
    the credentials have just worked, so nobody saves a typo."""
    user, account = request.user, request.account
    auth = SshAuth.KEY if private_key.strip() else SshAuth.PASSWORD
    default = f"{server.name} as {username}" if server is not None else f"{username}@{host}"
    name = (name or "").strip()[:80] or default[:80]
    with transaction.atomic():
        login = (
            SshLogin.objects.select_for_update()
            .filter(account=account, user=user, host=host, port=port, username=username)
            .first()
        )
        if login is None:
            if SshLogin.objects.filter(account=account, user=user).count() >= MAX_PER_USER:
                raise LoginError(f"You have {MAX_PER_USER} saved logins. Remove one first.")
            login = SshLogin(account=account, user=user, host=host, port=port,
                             username=username)  # fmt: skip
        login.server, login.name, login.auth = server, name, auth
        login.key_fingerprint = key_fingerprint if auth == SshAuth.KEY else ""
        login.last_used_at = timezone.now()
        login.secret = _seal(
            login, "" if auth == SshAuth.KEY else password,
            private_key if auth == SshAuth.KEY else "",
            passphrase if auth == SshAuth.KEY else "",
        )  # fmt: skip
        login.save()
    return login


def mine(request):
    """The signed-in person's saved logins in the current account. Nobody else's, ever."""
    return SshLogin.objects.filter(account=request.account, user=request.user)
