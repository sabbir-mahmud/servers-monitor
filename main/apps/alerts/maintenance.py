"""Which servers and applications are inside an active maintenance window."""

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import MaintenanceWindow


@dataclass
class Coverage:
    everything: bool = False
    server_ids: set = field(default_factory=set)
    monitor_ids: set = field(default_factory=set)
    windows: list = field(default_factory=list)

    def covers_server(self, server_id) -> bool:
        return self.everything or server_id in self.server_ids

    def covers_monitor(self, monitor_id, server_id=None) -> bool:
        # Muting a server also mutes the applications that run on it.
        return (
            self.everything
            or monitor_id in self.monitor_ids
            or (server_id is not None and server_id in self.server_ids)
        )

    def covers_alert(self, alert) -> bool:
        if alert.server_id:
            return self.covers_server(alert.server_id)
        if alert.monitor_id:
            return self.covers_monitor(alert.monitor_id, getattr(alert.monitor, "server_id", None))
        return False

    def window_for_server(self, server_id):
        for w in self.windows:
            if w.all_targets or server_id in w._server_ids:
                return w
        return None

    def window_for_monitor(self, monitor_id, server_id=None):
        for w in self.windows:
            if w.all_targets or monitor_id in w._monitor_ids or server_id in w._server_ids:
                return w
        return None


def coverage(at=None) -> Coverage:
    at = at or timezone.now()
    cov = Coverage()
    windows = MaintenanceWindow.objects.filter(starts_at__lte=at, ends_at__gt=at).prefetch_related(
        "servers", "monitors"
    )
    for w in windows:
        w._server_ids = {s.pk for s in w.servers.all()}
        w._monitor_ids = {m.pk for m in w.monitors.all()}
        cov.windows.append(w)
        cov.everything |= w.all_targets
        cov.server_ids |= w._server_ids
        cov.monitor_ids |= w._monitor_ids
    return cov


def was_covered(alert) -> bool:
    """Was the alert's target in maintenance when it opened? (Explains a late notice.)"""
    return coverage(alert.opened_at).covers_alert(alert)


@transaction.atomic
def quick_mute(user, minutes: int, *, server=None, monitor=None) -> MaintenanceWindow:
    target = server or monitor
    now = timezone.now()
    window = MaintenanceWindow.objects.create(
        title=f"Muted {target.name} for {minutes // 60}h"
        if minutes >= 60
        else f"Muted {target.name}",
        starts_at=now,
        ends_at=now + timedelta(minutes=minutes),
        created_by=user,
    )
    (window.servers if server else window.monitors).add(target)
    return window


def end_now(window: MaintenanceWindow) -> None:
    now = timezone.now()
    if window.ends_at > now:
        window.ends_at = max(now, window.starts_at + timedelta(seconds=1))
        window.save(update_fields=["ends_at"])
