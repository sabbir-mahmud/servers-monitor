"""Alert webhooks (Slack, Discord, Teams, Mattermost), sent from a short-lived thread with
retries and jittered backoff. No queue: a dashboard restart may drop a notification that
was mid-retry, which is acceptable for a nudge whose source of truth is the alerts page."""

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

RETRIES = 5


def _send(alert_id: str, event: str) -> None:
    from .models import Alert

    try:
        alert = Alert.objects.select_related("server").filter(pk=alert_id).first()
        if alert is None:  # deleted with its server meanwhile
            return
        icon = (
            "✅ RESOLVED" if event == "resolved" else f"🚨 {alert.get_severity_display().upper()}"
        )
        text = f"{icon}: {alert.message}"
    finally:
        close_old_connections()
    # `text` for Slack/Mattermost/Teams workflows, `content` for Discord.
    body = json.dumps({"text": text, "content": text}).encode()
    for attempt in range(RETRIES):
        req = urllib.request.Request(
            settings.ALERT_WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read(1024)
            return
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                logger.warning("Alert webhook rejected (%s); not retrying", exc.code)
                return
        except OSError as exc:  # DNS, refused, timeout
            logger.info("Alert webhook attempt %d failed: %s", attempt + 1, exc)
        time.sleep(min(600, 2**attempt * 5) * (0.5 + random.random()))
    logger.warning("Alert webhook gave up after %d attempts", RETRIES)


def notify(alert_id: str, event: str) -> None:
    if settings.ALERT_WEBHOOK_URL:
        threading.Thread(target=_send, args=(alert_id, event), daemon=True).start()
