from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from apps.audit.services import record


@receiver(user_logged_in, dispatch_uid="audit.login")
def on_login(sender, request, user, **kwargs):
    record(request, "auth.login", target=user.get_username())


@receiver(user_login_failed, dispatch_uid="audit.login_failed")
def on_login_failed(sender, credentials, request=None, **kwargs):
    # Username only; never the password.
    record(request, "auth.login_failed", target=str(credentials.get("username", ""))[:150])
