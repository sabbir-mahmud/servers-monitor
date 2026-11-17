"""Devices people carry or use: Windows, macOS and Linux computers, Android and iOS phones.

Unlike servers (which talk to the relay), devices check in over HTTPS: the desktop app
every few minutes, the phone app every 15 minutes or so (the OS decides). Each check-in
carries battery, storage, network and, when the person allowed it, the location.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


class Platform(models.TextChoices):
    WINDOWS = "windows", "Windows"
    MACOS = "macos", "Mac"
    LINUX = "linux", "Linux"
    ANDROID = "android", "Android"
    IOS = "ios", "iPhone or iPad"


COMPUTERS = (Platform.WINDOWS, Platform.MACOS, Platform.LINUX)
PHONES = (Platform.ANDROID, Platform.IOS)

LOW_BATTERY = 15  # percent, not charging

# How often each kind of device is asked to check in (the phone OS may stretch it).
CHECKIN_SECONDS = {"computer": 300, "phone": 900}


class DeviceState(models.TextChoices):
    WAITING = "waiting", "Waiting to pair"
    ONLINE = "online", "Online"
    OFFLINE = "offline", "Offline"


class MachineKind(models.TextChoices):
    SERVER = "server", "Linux server"
    DEVICE = "device", "Computer or phone"


class Machine(models.Model):
    """Everything an account watches, in one table: a Linux server (fleet.Server, through
    the agent and relay) or a computer or phone (Device, through the app).

    What each kind needs on top lives in its own table, joined one-to-one on the same id
    (multi-table inheritance), so `Machine.objects` lists both in one query while commands,
    metrics and alerts keep pointing at servers and location history at devices.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    account = models.ForeignKey(
        "tenants.Account", on_delete=models.CASCADE, related_name="machines"
    )
    kind = models.CharField(max_length=10, choices=MachineKind.choices, editable=False)
    name = models.CharField(max_length=100)
    hostname = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Each subclass names its kind; save() stamps it so the base row always says which
    # details table to join.
    KIND = ""

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["account", "kind", "name"], name="machine_account_kind")]
        constraints = [
            # One name per account across servers, computers and phones: they share a list.
            models.UniqueConstraint(
                "account",
                Lower("name"),
                name="machine_name_unique_per_account",
                violation_error_message="Another device or server in this account has that name.",
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.KIND:
            self.kind = self.KIND
        super().save(*args, **kwargs)

    @property
    def details(self):
        """The Server or Device behind this row (select_related("server", "device"))."""
        return self.server if self.kind == MachineKind.SERVER else self.device


class Device(Machine):
    KIND = MachineKind.DEVICE

    machine_ptr = models.OneToOneField(
        Machine,
        on_delete=models.CASCADE,
        parent_link=True,
        primary_key=True,
        db_column="id",  # the id it had before the base table existed
        related_name="device",
    )
    # Removing a person leaves their devices in the account, unassigned.
    person = models.ForeignKey(
        "tenants.Person",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="devices",
    )
    platform = models.CharField(max_length=10, choices=Platform.choices)

    # Pairing (see pairing.py). Empty: no code outstanding / not paired.
    pairing_code_hash = models.CharField(max_length=64, blank=True, editable=False)
    token_hash = models.CharField(max_length=64, blank=True, editable=False)
    paired_at = models.DateTimeField(null=True, blank=True)

    # Reported by the device (last_seen_at and hostname live on Machine).
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    os_version = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    app_version = models.CharField(max_length=30, blank=True)
    battery_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    charging = models.BooleanField(null=True, blank=True)
    storage_free = models.BigIntegerField(null=True, blank=True)
    storage_total = models.BigIntegerField(null=True, blank=True)
    # Health, computers only (phones don't report it): the latest reading. History below.
    cpu_percent = models.FloatField(null=True, blank=True)
    memory_used = models.BigIntegerField(null=True, blank=True)
    memory_total = models.BigIntegerField(null=True, blank=True)
    gpu_percent = models.FloatField(null=True, blank=True)
    network = models.CharField(max_length=20, blank=True)  # wifi, cellular, ethernet, none
    network_name = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_accuracy_m = models.PositiveIntegerField(null=True, blank=True)
    location_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # Lookups by code / token must hit exactly one device.
            models.UniqueConstraint(
                fields=["pairing_code_hash"],
                condition=~Q(pairing_code_hash=""),
                name="device_pairing_code_unique",
            ),
            models.UniqueConstraint(
                fields=["token_hash"], condition=~Q(token_hash=""), name="device_token_unique"
            ),
            models.CheckConstraint(
                condition=Q(platform__in=["windows", "macos", "linux", "android", "ios"]),
                name="device_platform_valid",
            ),
        ]

    @property
    def is_phone(self) -> bool:
        return self.platform in PHONES

    @property
    def form_factor(self) -> str:
        return "phone" if self.is_phone else "computer"

    @property
    def checkin_seconds(self) -> int:
        return CHECKIN_SECONDS[self.form_factor]

    @property
    def is_paired(self) -> bool:
        return bool(self.token_hash)

    @property
    def state(self) -> str:
        if not self.is_paired:
            return DeviceState.WAITING
        if self.last_seen_at is None:
            return DeviceState.OFFLINE
        # Two missed check-ins (plus slack for a phone OS delaying background work).
        limit = timedelta(seconds=self.checkin_seconds * 2 + 120)
        return (
            DeviceState.ONLINE
            if timezone.now() - self.last_seen_at <= limit
            else (DeviceState.OFFLINE)
        )

    @property
    def state_label(self) -> str:
        return DeviceState(self.state).label

    @property
    def battery_low(self) -> bool:
        return (
            self.battery_percent is not None
            and self.battery_percent <= LOW_BATTERY
            and not self.charging
        )

    @property
    def attention(self) -> str:
        """Why a parent should look at this device, or "" if all is well."""
        if self.state == DeviceState.OFFLINE:
            return "Offline"
        if self.state == DeviceState.ONLINE and self.battery_low:
            return "Battery low"
        return ""

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def storage_used_percent(self) -> int | None:
        if not self.storage_total or self.storage_free is None:
            return None
        return round(100 * (self.storage_total - self.storage_free) / self.storage_total)

    @property
    def memory_used_percent(self) -> int | None:
        if not self.memory_total or self.memory_used is None:
            return None
        return round(100 * self.memory_used / self.memory_total)

    @property
    def has_health(self) -> bool:
        return self.cpu_percent is not None or self.memory_total is not None


class LocationSource(models.TextChoices):
    GPS = "gps", "GPS"
    NETWORK = "network", "Wi-Fi / cell"
    IP = "ip", "IP address"


class DeviceLocation(models.Model):
    """Location history. Pruned after DEVICE_LOCATION_DAYS; deleted with the device."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="locations")
    recorded_at = models.DateTimeField()
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=LocationSource.choices, default="gps")

    account_path = "device__account"

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["device", "-recorded_at"], name="devloc_device_recent")]

    def __str__(self):
        return f"{self.device_id} at {self.recorded_at:%Y-%m-%d %H:%M}"


class DeviceSample(models.Model):
    """One check-in's health reading, for the device's History charts. Each value is
    optional: a phone has no CPU figure, a desktop no battery. Pruned after
    DEVICE_METRICS_DAYS; deleted with the device."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="samples")
    recorded_at = models.DateTimeField()
    cpu_percent = models.FloatField(null=True, blank=True)
    memory_percent = models.FloatField(null=True, blank=True)
    gpu_percent = models.FloatField(null=True, blank=True)
    battery_percent = models.FloatField(null=True, blank=True)
    disk_percent = models.FloatField(null=True, blank=True)

    account_path = "device__account"
    METRICS = ("cpu_percent", "memory_percent", "gpu_percent", "battery_percent", "disk_percent")

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["device", "recorded_at"], name="devsample_device_time")]

    def __str__(self):
        return f"{self.device_id} at {self.recorded_at:%Y-%m-%d %H:%M}"


def summarize(devices) -> dict:
    """One person's devices, condensed for their card: is everything fine, when were they
    last active and where. Works on an already-fetched list (no queries)."""
    devices = list(devices)
    paired = [d for d in devices if d.is_paired]
    seen = [d for d in paired if d.last_seen_at]
    located = [d for d in paired if d.has_location and d.location_at]
    attention = [d for d in devices if d.attention]
    online = sum(1 for d in devices if d.state == DeviceState.ONLINE)
    waiting = sum(1 for d in devices if d.state == DeviceState.WAITING)
    if not devices:
        tone, headline = "muted", "No devices yet"
    elif attention:
        tone = "critical" if any(d.attention == "Offline" for d in attention) else "warning"
        headline = (
            f"{attention[0].name}: {attention[0].attention.lower()}"
            if len(attention) == 1
            else f"{len(attention)} devices need a look"
        )
    elif online:
        tone = "good"
        headline = (
            "All devices online"
            if online == len(paired) and paired
            else (f"{online} of {len(devices)} online")
        )
    else:
        tone, headline = "muted", "Waiting to pair"
    return {
        "devices": devices,
        "total": len(devices),
        "online": online,
        "waiting": waiting,
        "attention": attention,
        "tone": tone,
        "headline": headline,
        "last_active": max(seen, key=lambda d: d.last_seen_at, default=None),
        "last_location": max(located, key=lambda d: d.location_at, default=None),
        "lowest_battery": min(
            (d for d in paired if d.battery_percent is not None),
            key=lambda d: d.battery_percent,
            default=None,
        ),
    }
