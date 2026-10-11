"""Outbound HTTP that can't be pointed at this machine's private network (SSRF guard).

Application health checks and webhooks fetch URLs that Admins type in. Unguarded, that turns
the dashboard into a probe for internal services: 127.0.0.1, the router, other LAN hosts,
and cloud metadata (169.254.169.254), with the keyword check acting as an oracle for what
those pages contain.

The check runs on the address actually connected to, after DNS resolution and for every
redirect hop, so a hostname that resolves publicly and then rebinds to 127.0.0.1 is still
caught. Only http and https are spoken; no proxies, ftp, file: or data: handlers exist.

MONITOR_ALLOW_PRIVATE_TARGETS=True permits private and loopback addresses (monitoring a
home LAN is a legitimate use). Link-local addresses, where cloud metadata lives, stay
blocked either way.
"""

import http.client
import ipaddress
import socket
import ssl
import urllib.request

from django.conf import settings


class BlockedAddress(OSError):
    """The destination is an internal address this dashboard may not contact."""


def is_allowed(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return False  # never: cloud metadata (169.254.169.254) and other oddities
    if addr.is_global:
        return True
    return bool(settings.MONITOR_ALLOW_PRIVATE_TARGETS)


def describe_block(ip: str) -> str:
    hint = (
        ""
        if ipaddress.ip_address(ip).is_link_local
        else " (set MONITOR_ALLOW_PRIVATE_TARGETS=True to monitor internal services)"
    )
    return f"blocked: {ip} is an internal address{hint}"


def create_connection(
    address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs
):
    """socket.create_connection, but internal addresses are refused before any connection
    is attempted (so "refused" vs "blocked" can't map which internal ports are open), and
    we connect to the exact address that was checked (no DNS rebinding in between)."""
    host, port = address[0], address[1]
    blocked, last_error = None, None
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = sockaddr[0]
        if not is_allowed(ip):
            blocked = ip
            continue
        try:
            return socket.create_connection((ip, port), timeout, source_address, *args, **kwargs)
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise BlockedAddress(describe_block(blocked)) if blocked else OSError(f"cannot resolve {host}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_connection  # checked before the TLS handshake


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            raise BlockedAddress(f"blocked: redirect to a non-HTTP address ({newurl[:60]})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(
    context: ssl.SSLContext | None = None, follow_redirects: bool = True
) -> urllib.request.OpenerDirector:
    """An opener with only guarded http/https, redirects and error handling: no proxy,
    ftp, file or data handlers (build_opener would add those). Without redirects, a 3xx
    surfaces as an HTTPError carrying its status."""
    director = urllib.request.OpenerDirector()
    handlers = [
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _HTTPHandler(),
        _HTTPSHandler(context=context),
    ]
    if follow_redirects:
        handlers.append(_RedirectHandler())
    for handler in handlers:
        director.add_handler(handler)
    return director


def urlopen(
    request, *, timeout: float, context: ssl.SSLContext | None = None, follow_redirects: bool = True
):
    return opener(context, follow_redirects).open(request, timeout=timeout)


def check_url_host(url: str) -> str | None:
    """Form-time check: an error message if every address the host resolves to is blocked.
    (The connect-time check above is the real guard; this just fails early and clearly.)"""
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname
    if not host:
        return "That URL has no host."
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None  # may resolve later; the check itself will report DNS failures
    ips = {info[4][0] for info in infos}
    blocked = [ip for ip in ips if not is_allowed(ip)]
    if blocked and len(blocked) == len(ips):
        return describe_block(blocked[0]).replace("blocked: ", "This host is blocked: ")
    return None
