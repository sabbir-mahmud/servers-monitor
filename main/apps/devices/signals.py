"""A removed phone loses its relay login, however it was removed (its page, or with its
person or account)."""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Device


@receiver(post_delete, sender=Device)
def remove_relay_login(sender, instance: Device, **kwargs) -> None:
    if instance.relay_salt:
        from apps.agent_api import relay

        relay.deprovision_device(instance)
