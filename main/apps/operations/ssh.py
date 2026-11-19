"""SSH from the browser: the dashboard is the SSH client, the page is the screen.

The browser never speaks SSH. It posts the connection details once (`open`), then
long-polls for output (`read`, up to 20 s per request) and posts keystrokes (`write`) and
window sizes (`resize`). Each session is an interactive shell on a PTY held in this
process's memory, so the web server must be one process (it is: gunicorn --workers 1
--threads 8). Every long-poll holds a thread, which is why sessions are capped.

Safety:
- Only the person who opened a session can read from or type into it.
- The password or private key is used for that one connection and never stored or logged.
- The dashboard's own keys and SSH agent are never offered (look_for_keys/allow_agent off).
- Host keys are trusted on first use, per account, and must match afterwards: a changed key
  refuses the connection until someone deliberately forgets the old one.
- The address goes through the same guard as health checks (apps/core/netguard): cloud
  metadata never; private and loopback addresses only with SSH_ALLOW_PRIVATE_TARGETS.
- Opening and closing are audited (who, where, how long), not the keystrokes.
- Idle sessions close: nobody polling for a minute (tab closed), or nothing typed for
  SSH_IDLE_MINUTES.
"""

import base64
import io
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field

import paramiko
from django.conf import settings
from django.db import IntegrityError

from apps.core import netguard

logger = logging.getLogger(__name__)

BUFFER_LIMIT = 512 * 1024  # bytes of output kept for a slow reader; older bytes are dropped
POLL_SECONDS = 20
ABANDONED_SECONDS = 60  # no read request for this long: the page is gone
CONNECT_TIMEOUT = 12


class SshError(Exception):
    """Something to show the person: why the connection didn't open."""


class HostKeyChanged(SshError):
    pass


@dataclass
class Session:
    id: str
    user_id: int
    account_id: object
    label: str  # user@host:port
    client: paramiko.SSHClient
    channel: paramiko.Channel
    opened_at: float = field(default_factory=time.monotonic)
    buffer: bytearray = field(default_factory=bytearray)
    base: int = 0  # stream offset of buffer[0]
    closed: bool = False
    reason: str = ""
    last_read: float = field(default_factory=time.monotonic)
    last_input: float = field(default_factory=time.monotonic)
    cond: threading.Condition = field(default_factory=threading.Condition)
    on_close: object = None  # called once with the reason (the audit entry)

    @property
    def end(self) -> int:
        return self.base + len(self.buffer)


_sessions: dict[str, Session] = {}
_lock = threading.Lock()
_reaper_started = False


# --------------------------------------------------------------------------- host keys


def _fingerprint(key: paramiko.PKey) -> str:
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Remember the key the first time; afterwards it must match."""

    def __init__(self, account_id, host: str, port: int):
        self.account_id, self.host, self.port = account_id, host, port
        self.fingerprint = ""

    def missing_host_key(self, client, hostname, key):
        from .models import SshHostKey

        self.fingerprint = _fingerprint(key)
        known = SshHostKey.objects.filter(account_id=self.account_id, host=self.host,
                                          port=self.port).first()  # fmt: skip
        if known is None:
            try:
                SshHostKey.objects.create(account_id=self.account_id, host=self.host,
                                          port=self.port, key_type=key.get_name(),
                                          fingerprint=self.fingerprint)  # fmt: skip
            except IntegrityError:
                pass
            return
        if known.fingerprint != self.fingerprint:
            raise HostKeyChanged(
                f"The host key of {self.host} changed (now {self.fingerprint}, saved "
                f"{known.fingerprint}). That happens after a reinstall, or when someone is "
                "intercepting the connection. If you're sure it's the reinstall, forget the "
                "saved key and connect again."
            )


# --------------------------------------------------------------------------- open


def _load_key(text: str, passphrase: str | None) -> paramiko.PKey:
    text = text.strip() + "\n"
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise SshError("This key is protected: type its passphrase.") from exc
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(exc)
    raise SshError("That doesn't look like a private key NexGuard can read (OpenSSH or PEM, "
                   "Ed25519, ECDSA or RSA).")  # fmt: skip


def _socket(host: str, port: int) -> socket.socket:
    allow_private = settings.SSH_ALLOW_PRIVATE_TARGETS
    try:
        return netguard.create_connection((host, port), timeout=CONNECT_TIMEOUT,
                                          allow_private=allow_private)  # fmt: skip
    except netguard.BlockedAddress as exc:
        detail = (
            f"{host} is an address on a private network. To allow SSH to your own "
            "network, set SSH_ALLOW_PRIVATE_TARGETS=True."  # fmt: skip
            if not allow_private
            else f"{host} can't be reached from here (blocked)."
        )
        raise SshError(detail) from exc
    except TimeoutError as exc:
        raise SshError(f"{host}:{port} didn't answer (timed out). Is SSH listening, and is the "
                       "port open to this dashboard?") from exc  # fmt: skip
    except OSError as exc:
        raise SshError(f"Couldn't reach {host}:{port}: {exc.strerror or exc}") from exc


def open_session(*, user, account, host: str, port: int, username: str, password: str = "",
                 private_key: str = "", passphrase: str = "", cols: int = 120,
                 rows: int = 32) -> Session:  # fmt: skip
    _start_reaper()
    with _lock:
        mine = sum(1 for s in _sessions.values() if s.user_id == user.pk and not s.closed)
        if mine >= settings.SSH_MAX_SESSIONS_PER_USER:
            raise SshError("You already have the most terminals open. Close one first.")
        if sum(1 for s in _sessions.values() if not s.closed) >= settings.SSH_MAX_SESSIONS:
            raise SshError("Too many terminals are open on this dashboard. Try again soon.")
    pkey = _load_key(private_key, passphrase) if private_key.strip() else None
    if pkey is None and not password:
        raise SshError("Type the password, or paste a private key.")
    sock = _socket(host, port)
    client = paramiko.SSHClient()
    policy = _TrustOnFirstUse(account.pk, host, port)
    client.set_missing_host_key_policy(policy)
    # Known hosts come only from the account's saved keys (the policy): never the
    # dashboard machine's ~/.ssh/known_hosts.
    try:
        client.connect(
            hostname=host, port=port, username=username, password=password or None,
            pkey=pkey, sock=sock, timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT, look_for_keys=False, allow_agent=False,
        )  # fmt: skip
    except HostKeyChanged:
        client.close()
        raise
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SshError(f"{username}@{host} refused the {'key' if pkey else 'password'}.") from exc
    except (paramiko.SSHException, OSError, EOFError) as exc:
        client.close()
        raise SshError(f"SSH to {host}:{port} failed: {exc}") from exc
    try:
        channel = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
    except paramiko.SSHException as exc:
        client.close()
        raise SshError(f"{host} didn't open a shell: {exc}") from exc
    session = Session(id=secrets.token_urlsafe(18), user_id=user.pk, account_id=account.pk,
                      label=f"{username}@{host}:{port}", client=client, channel=channel)  # fmt: skip
    with _lock:
        _sessions[session.id] = session
    threading.Thread(target=_pump, args=(session,), daemon=True,
                     name=f"ssh-{session.id[:6]}").start()  # fmt: skip
    return session


def _pump(session: Session) -> None:
    """Copy the shell's output into the session buffer until it ends."""
    channel = session.channel
    try:
        while True:
            data = channel.recv(32768)
            if not data:
                break
            with session.cond:
                session.buffer += data
                if len(session.buffer) > BUFFER_LIMIT:
                    drop = len(session.buffer) - BUFFER_LIMIT
                    del session.buffer[:drop]
                    session.base += drop
                session.cond.notify_all()
    except OSError, EOFError, paramiko.SSHException:
        pass
    _finish(session, "The connection closed.")


def _finish(session: Session, reason: str) -> None:
    with session.cond:
        if session.closed:
            return
        session.closed, session.reason = True, reason
        session.cond.notify_all()
    try:
        session.client.close()
    except Exception:  # noqa: BLE001 - closing is best effort
        pass
    if callable(session.on_close):
        try:
            session.on_close(reason)
        except Exception:  # noqa: BLE001 - an audit hiccup mustn't keep a session open
            logger.exception("Couldn't record the end of SSH session %s", session.label)


# --------------------------------------------------------------------------- use


def get(session_id: str, user) -> Session | None:
    with _lock:
        session = _sessions.get(session_id)
    if session is None or session.user_id != user.pk:
        return None  # someone else's terminal looks exactly like no terminal
    return session


def read(session: Session, offset: int, timeout: float = POLL_SECONDS) -> dict:
    """Output after `offset` (waiting up to `timeout` for some), as base64."""
    deadline = time.monotonic() + timeout
    with session.cond:
        session.last_read = time.monotonic()
        while session.end <= offset and not session.closed:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            session.cond.wait(left)
        start = max(offset, session.base)
        data = bytes(session.buffer[start - session.base :])
        session.last_read = time.monotonic()
        return {
            "data": base64.b64encode(data).decode(),
            "offset": session.end,
            "skipped": start > offset,  # the reader was too slow: some output was dropped
            "closed": session.closed and session.end <= start + len(data),
            "reason": session.reason,
        }


def write(session: Session, text: str) -> None:
    if session.closed:
        return
    session.last_input = time.monotonic()
    try:
        session.channel.sendall(text.encode())
    except OSError, EOFError, paramiko.SSHException:
        _finish(session, "The connection closed.")


def resize(session: Session, cols: int, rows: int) -> None:
    if not session.closed and 10 <= cols <= 500 and 3 <= rows <= 200:
        try:
            session.channel.resize_pty(width=cols, height=rows)
        except OSError, paramiko.SSHException:
            pass


def close(session: Session, reason: str = "You closed the terminal.") -> None:
    _finish(session, reason)


def duration(session: Session) -> int:
    return int(time.monotonic() - session.opened_at)


# --------------------------------------------------------------------------- reaper


def _start_reaper() -> None:
    global _reaper_started
    with _lock:
        if _reaper_started:
            return
        _reaper_started = True
    threading.Thread(target=_reap_forever, daemon=True, name="ssh-reaper").start()


def reap(now: float | None = None) -> int:
    """Close abandoned and idle sessions, and forget closed ones. Returns how many closed."""
    now = now or time.monotonic()
    idle = settings.SSH_IDLE_MINUTES * 60
    closed = 0
    with _lock:
        sessions = list(_sessions.values())
    for s in sessions:
        if s.closed:
            if now - s.last_read > ABANDONED_SECONDS:
                with _lock:
                    _sessions.pop(s.id, None)
            continue
        if now - s.last_read > ABANDONED_SECONDS:
            _finish(s, "Closed: the page was closed.")
            closed += 1
        elif now - s.last_input > idle:
            _finish(s, f"Closed after {settings.SSH_IDLE_MINUTES} minutes without typing.")
            closed += 1
    return closed


def _reap_forever() -> None:
    while True:
        time.sleep(15)
        try:
            reap()
        except Exception:  # noqa: BLE001 - keep reaping
            logger.exception("SSH reaper failed")
