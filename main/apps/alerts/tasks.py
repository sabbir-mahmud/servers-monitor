import json
import logging
import urllib.error
import urllib.request

from celery import shared_task
from django.conf import settings

from .models import Alert
from .services import sweep

logger = logging.getLogger(__name__)


class WebhookTransientError(Exception):
    pass


@shared_task(name="alerts.sweep", acks_late=True, ignore_result=True)
def sweep_task():
    result = sweep()
    if any(result.values()):
        logger.info("Sweep: %s", result)
    return result


@shared_task(
    bind=True,
    name="alerts.notify",
    acks_late=True,
    max_retries=5,
    autoretry_for=(WebhookTransientError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def notify_alert(self, alert_id: str, event: str):
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return
    alert = Alert.objects.select_related("server").filter(pk=alert_id).first()
    if alert is None:  # deleted with its server since enqueue
        return
    icon = "✅ RESOLVED" if event == "resolved" else f"🚨 {alert.get_severity_display().upper()}"
    text = f"{icon}: {alert.message}"
    # `text` for Slack/Mattermost/Teams-workflows, `content` for Discord.
    body = json.dumps({"text": text, "content": text}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read(1024)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code == 429:
            raise WebhookTransientError(f"webhook {exc.code}") from exc
        logger.warning("Alert webhook rejected (%s); not retrying", exc.code)
    except OSError as exc:  # DNS, refused, timeout
        raise WebhookTransientError(str(exc)) from exc
