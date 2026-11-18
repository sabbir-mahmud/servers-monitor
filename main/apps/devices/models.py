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

# app_version prefix the Go device agent reports (device-agent/internal/collect).
AGENT_APP = "nexguard-agent "
# Whose computers can have screen time, web activity and blocking (Person.kind values).
CONTROLLED_PEOPLE = ("child", "staff")

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
    # The Go agent: "system" (a service an administrator installed: can block sites) or
    # "user" (installed by the one-line connect command as the person: can't). "" = not it.
    agent_mode = models.CharField(max_length=10, blank=True)
    # Browsers the agent isn't allowed to read (macOS: until it has Full Disk Access).
    web_no_access = models.JSONField(default=list, blank=True)
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

    @property
    def controls_block_reason(self) -> str:
        """Why screen time, web activity and blocking can't be turned on ("" if they can).
        Only your own computers (a solo account), a child's or a staff member's: on another
        adult's computer this is how stalkerware is abused. Only the agent: it runs as a
        service the user can't remove, and only it can read other users' browsers or edit
        the hosts file."""
        if self.platform not in COMPUTERS:
            return "Only for Windows, Mac and Linux computers."
        if not self.controls_owner_ok:
            return "Only for a child's or a staff member's computer."
        if not self.app_version.startswith(AGENT_APP):
            return "Needs the NexGuard agent: pair this computer again with the one-line command."
        return ""

    @property
    def controls_owner_ok(self) -> bool:
        """Whose computer it is allows controls: a solo developer's own, or one assigned to
        a child or a staff member (select_related("account", "person") avoids queries)."""
        if self.account.kind == "solo":
            return True
        return self.person is not None and self.person.kind in CONTROLLED_PEOPLE

    @property
    def can_block(self) -> bool:
        """Blocking edits the hosts file, which only an administrator-installed agent can."""
        return self.controls_allowed and self.agent_mode != "user"

    @property
    def controls_allowed(self) -> bool:
        return not self.controls_block_reason


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


# ---------------------------------------------------------------- controls and activity


class DeviceControls(models.Model):
    """What the agent on a child's or staff member's computer does beyond health. Created
    the first time someone saves them; no row means everything off."""

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="controls")
    screen_time = models.BooleanField(default=False, help_text="Time in use, by app.")
    web_activity = models.BooleanField(
        default=False, help_text="Websites visited, as domains with visits and time."
    )
    block_adult = models.BooleanField(default=False, help_text="Block adult websites.")
    safe_search = models.BooleanField(
        default=False, help_text="Force SafeSearch on Google, Bing, DuckDuckGo and YouTube."
    )
    blocked_domains = models.TextField(blank=True, help_text="One site per line.")
    allowed_domains = models.TextField(
        blank=True, help_text="Never blocked, even if on the adult list. One per line."
    )
    updated_at = models.DateTimeField(auto_now=True)

    account_path = "device__account"

    def __str__(self):
        return f"Controls for {self.device_id}"

    @property
    def blocking(self) -> bool:
        return self.block_adult or self.safe_search or bool(self.blocked_domains.strip())


class ScreenTime(models.Model):
    """Seconds a computer was in use (signed in, unlocked, not idle) per day, OS user and
    app in front. Check-ins add to the row; pruned after DEVICE_ACTIVITY_DAYS."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="screen_time")
    day = models.DateField()
    os_user = models.CharField(max_length=100)
    app = models.CharField(max_length=100)
    seconds = models.PositiveIntegerField(default=0)

    account_path = "device__account"

    class Meta:
        ordering = ["-day", "-seconds"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "day", "os_user", "app"], name="screentime_unique"
            ),
            models.CheckConstraint(condition=Q(seconds__lte=86400), name="screentime_max_day"),
        ]
        indexes = [models.Index(fields=["day"], name="screentime_day")]  # pruning

    def __str__(self):
        return f"{self.app} {self.seconds}s on {self.day}"


class WebActivity(models.Model):
    """Visits and time per website (domain only, never the page) per day, OS user and
    browser. Check-ins add to the row; pruned after DEVICE_ACTIVITY_DAYS."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="web_activity")
    day = models.DateField()
    os_user = models.CharField(max_length=100)
    browser = models.CharField(max_length=50)
    domain = models.CharField(max_length=253)
    visits = models.PositiveIntegerField(default=0)
    seconds = models.PositiveIntegerField(default=0)

    account_path = "device__account"

    class Meta:
        ordering = ["-day", "-seconds", "-visits"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "day", "os_user", "browser", "domain"], name="webactivity_unique"
            ),
        ]
        indexes = [models.Index(fields=["day"], name="webactivity_day")]  # pruning

    def __str__(self):
        return f"{self.domain} on {self.day}"


class ActivityBatch(models.Model):
    """Batch ids already counted: an agent resends a batch whose answer it never got, and
    the counters above only add, so each batch must count once."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="+")
    batch_id = models.UUIDField()
    received_at = models.DateTimeField(default=timezone.now)

    account_path = "device__account"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device", "batch_id"], name="activitybatch_unique")
        ]
        indexes = [models.Index(fields=["received_at"], name="activitybatch_received")]

    def __str__(self):
        return str(self.batch_id)
