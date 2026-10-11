"""4.4: installable app (manifest, service worker, offline page) and browser push."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from django.urls import reverse

from apps.alerts import channels, delivery, webpush
from apps.alerts.models import Alert, AlertKind, Channel, DeliveryEvent, Severity

from .test_channels import _form

pytestmark = pytest.mark.django_db


def key_from(b64):
    return ec.derive_private_key(int.from_bytes(webpush.unb64u(b64), "big"), ec.SECP256R1())


def test_encryption_matches_rfc_8291_example():
    """RFC 8291 §5: the exact bytes, from the RFC's own keys, salt and plaintext."""
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        webpush.unb64u(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
        ),
        webpush.unb64u("BTBZMqHH6r4Tts7J_aSIgg"),
        as_private=key_from("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        salt=webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert webpush.b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS"
        "6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Q"
        "ulcy4a-fN"
    )


def test_vapid_jwt_is_signed_by_this_installs_key():
    header = webpush.vapid_header(
        "https://fcm.googleapis.com/fcm/send/abc", "mailto:ops@example.com", now=1000
    )
    token, key = header.removeprefix("vapid t=").split(", k=")
    head, claims, sig = token.split(".")
    assert json.loads(webpush.unb64u(claims)) == {"aud": "https://fcm.googleapis.com", "exp": 1000 + 12 * 3600,
                                                  "sub": "mailto:ops@example.com"}  # fmt: skip
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.unb64u(key))
    raw = webpush.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if forged
    assert webpush.vapid_keys()[1] == key  # one key pair per install, reused


def test_vapid_private_key_is_encrypted_at_rest():
    from apps.core.models import SystemState

    key, _ = webpush.vapid_keys()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()  # fmt: skip
    assert pem.splitlines()[1] not in json.dumps(SystemState.get("vapid"))


class _PushService(BaseHTTPRequestHandler):
    got, status = [], 201

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _PushService.got.append({"headers": dict(self.headers), "body": body})
        self.send_response(_PushService.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def push_service(settings):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = True
    _PushService.got, _PushService.status = [], 201
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _PushService)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/push/abc"
    httpd.shutdown()


@pytest.fixture
def browser():
    """A browser's push subscription keys (what PushManager.subscribe() creates)."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)  # fmt: skip
    auth = b"0123456789abcdef"
    return key, webpush.b64u(public), webpush.b64u(auth), auth


def test_push_delivers_an_encrypted_alert_only_the_browser_can_read(push_service, browser, server):
    key, p256dh, auth_b64, auth = browser
    ch = Channel.objects.create(name="phone", kind="webpush",
                                config=json.dumps({"endpoint": push_service, "p256dh": p256dh, "auth": auth_b64}))  # fmt: skip
    alert = Alert.objects.create(
        server=server, kind=AlertKind.DISK, severity=Severity.CRITICAL, message="Disk at 97%"
    )
    delivery.send(ch, delivery.compose(alert, DeliveryEvent.OPENED))
    (req,) = _PushService.got
    assert b"Disk at 97%" not in req["body"]  # the push service only sees ciphertext
    assert req["headers"]["Content-Encoding"] == "aes128gcm" and req["headers"]["Urgency"] == "high"
    assert req["headers"]["Authorization"].startswith("vapid t=")
    shown = json.loads(webpush.decrypt(req["body"], key, auth))
    assert "Disk at 97%" in shown["body"] and shown["severity"] == "critical"


def test_expired_subscription_says_to_resubscribe(push_service, browser):
    _PushService.status = 410
    _, p256dh, auth_b64, _ = browser
    msg = channels.Message(title="t", body="b", plain="b", html="b", event="test")
    with pytest.raises(channels.SendError, match="subscribe it again"):
        channels.send_webpush({"endpoint": push_service, "p256dh": p256dh, "auth": auth_b64}, msg)


def test_push_channel_form(client, admin_user, browser):
    client.force_login(admin_user)
    page = client.get(reverse("alerts:channel_create") + "?kind=webpush").content.decode()
    assert "data-push-subscribe" in page and f'data-vapid="{webpush.vapid_keys()[1]}"' in page
    resp = client.post(reverse("alerts:channel_create") + "?kind=webpush", _form("webpush"))
    assert "Subscribe this browser first" in resp.content.decode() and not Channel.objects.exists()
    _, p256dh, auth_b64, _ = browser
    endpoint = "https://fcm.googleapis.com/fcm/send/token-123"
    resp = client.post(reverse("alerts:channel_create") + "?kind=webpush",
                       _form("webpush", endpoint=endpoint, p256dh=p256dh, auth=auth_b64))  # fmt: skip
    assert resp.status_code == 302
    ch = Channel.objects.get()
    assert ch.settings == {"endpoint": endpoint, "p256dh": p256dh, "auth": auth_b64}
    page = client.get(reverse("alerts:channel_update", args=[ch.pk])).content.decode()
    assert "token-123" not in page and p256dh not in page


def test_manifest_service_worker_and_offline_page(client, admin_user):
    manifest = client.get("/manifest.webmanifest")
    assert manifest["Content-Type"] == "application/manifest+json"
    data = manifest.json()
    assert data["display"] == "standalone" and any(
        i.get("purpose") == "maskable" for i in data["icons"]
    )
    sw = client.get("/sw.js")
    assert sw["Content-Type"].startswith("text/javascript") and sw["Service-Worker-Allowed"] == "/"
    body = sw.content.decode()
    assert 'addEventListener("push"' in body and "/offline/" in body
    assert "Offline" in client.get("/offline/").content.decode()  # works signed out
    client.force_login(admin_user)
    page = client.get(reverse("fleet:dashboard")).content.decode()
    assert 'rel="manifest"' in page and "data-install-app" in page


def test_icons_are_real_pngs():
    from django.conf import settings

    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        data = (settings.BASE_DIR / "static" / "img" / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
