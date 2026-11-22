"""A removed computer or phone loses its relay login, however it was removed (its page, or
with its person or account)."""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Device


@receiver(post_delete, sender=Device)
def remove_relay_login(sender, instance: Device, **kwargs) -> None:
    # Always, not only when `relay_salt` says it was paired: an instance loaded before it
    # paired would otherwise leave a working login behind. Deleting a user that doesn't
    # exist is harmless, and failures are only logged.
    from apps.agent_api import relay

    relay.deprovision_device(instance)
