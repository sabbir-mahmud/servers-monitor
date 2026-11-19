import ipaddress
from datetime import date, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.audit.services import record
from apps.core import crypto
from apps.core.mixins import StaffViewMixin
from apps.tenants.models import Person
from apps.tenants.scoping import AccountScopedMixin, scoped

from . import activity, connect, maps, services
from .forms import DeviceControlsForm, DeviceForm, PlaceForm
from .models import COMPUTERS, Device, DeviceControls, DevicePlace, Place

CODE_SESSION_KEY = "device-code:{}"


def _remember_code(request, device, code):
    """Shown on the next page load only; kept encrypted in the session until then."""
    request.session[CODE_SESSION_KEY.format(device.pk)] = crypto.encrypt(code)


def _back_to(device) -> str:
    if device.person_id:
        return reverse("tenants:person_detail", args=[device.person_id])
    if device.account.kind == "solo":
        return reverse("fleet:dashboard")  # a solo developer's devices sit with their servers
    return reverse("tenants:people")


class DeviceCreateView(StaffViewMixin, AccountScopedMixin, CreateView):
    permission_required = "devices.add_device"
    model = Device
    form_class = DeviceForm
    template_name = "devices/form.html"

    def get_initial(self):
        initial = super().get_initial()
        person = scoped(self.request, Person).filter(pk=self.request.GET.get("person") or None)
        if (p := person.first()) is not None:
            initial["person"] = p
        if self.request.GET.get("platform"):
            initial["platform"] = self.request.GET["platform"]
        return initial

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        device = form.save()
        code = services.new_pairing_code(device)
        _remember_code(self.request, device, code)
        record(self.request, "device.create", target=device.name, platform=device.platform,
               person=device.person.name if device.person else "")  # fmt: skip
        return redirect("devices:pair", pk=device.pk)


class DevicePairView(StaffViewMixin, AccountScopedMixin, DetailView):
    """The pairing code, a QR code for the phone app, and how to install the app. Polls
    until the device pairs. POST makes a new code (lost it, or the device was reset)."""

    permission_required = "devices.add_device"
    model = Device
    template_name = "devices/pair.html"
    context_object_name = "device"

    def get_template_names(self):
        if self.request.htmx and self.request.GET.get("part") == "status":
            return [f"{self.template_name}#status"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        device = self.object
        code = None
        if self.request.GET.get("part") != "status":  # a poll must not use up the code
            stored = self.request.session.pop(CODE_SESSION_KEY.format(device.pk), None)
            code = crypto.decrypt(stored) if stored else None
        server = (settings.DASHBOARD_URL or self.request.build_absolute_uri("/")).rstrip("/")
        is_computer = device.platform in COMPUTERS
        if is_computer and code:
            # The link to open on the computer, and the one line it shows (see connect.py).
            ctx.update(connect.connect_context(server, code), only=device.platform)
        ctx.update(
            code=code,
            server_url=server,
            qr=_qr(server, code) if code and not is_computer else "",
            **(_install_link(server, device.platform) if code and not is_computer else {}),
            # The phone app can't reach the dashboard computer's own loopback address.
            server_is_loopback=urlsplit(server).hostname in ("localhost", "127.0.0.1", "::1"),
            is_computer=is_computer,
            # Paired before and a new code is out: it's being moved to a new/reset device.
            repairing=device.paired_at is not None and bool(device.pairing_code_hash),
            back_url=_back_to(device),
        )
        return ctx

    def post(self, request, pk):
        device = self.get_object()
        if request.POST.get("action") == "cancel":
            services.cancel_pairing_code(device)
            record(request, "device.code_cancel", target=device.name)
            messages.success(request, "Pairing code cancelled.")
            return redirect("devices:detail", pk=device.pk)
        code = services.new_pairing_code(device)
        _remember_code(request, device, code)
        record(request, "device.new_code", target=device.name)
        return redirect("devices:pair", pk=device.pk)


def _qr_svg(data: str, scale: int = 5) -> str:
    import segno

    return segno.make(data, error="m").svg_inline(scale=scale, border=2, dark="#1d1d1f",
                                                  light="#ffffff")  # fmt: skip


def _qr(server: str, code: str) -> str:
    """What the NexGuard phone app scans: where to pair and with which code."""
    from urllib.parse import urlencode

    return _qr_svg(
        "nexguard://pair?" + urlencode({"server": server, "code": code.replace("-", "")})
    )


def _install_link(server: str, platform: str) -> dict:
    """Where the phone gets the app: the Android build straight from this dashboard, or the
    iPhone link (TestFlight or the App Store). A QR code for the phone's own camera."""
    if platform == "android" and connect.android_apk():
        url = server + reverse("phone_app_android")
    elif platform == "ios" and settings.IOS_APP_URL:
        url = settings.IOS_APP_URL
    else:
        return {"app_install_url": ""}
    return {"app_install_url": url, "app_install_qr": _qr_svg(url, scale=3)}


class DeviceDetailView(StaffViewMixin, AccountScopedMixin, DetailView):
    permission_required = "devices.view_device"
    model = Device
    template_name = "devices/detail.html"
    context_object_name = "device"

    def get_queryset(self):
        return super().get_queryset().select_related("account", "person")

    def get_template_names(self):
        if self.request.htmx and self.request.GET.get("part") == "charts":  # range / refresh
            return [f"{self.template_name}#charts"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        device = self.object
        range_key = self.request.GET.get("range", services.DEFAULT_CHART_RANGE)
        if range_key not in services.CHART_RANGES:
            range_key = services.DEFAULT_CHART_RANGE
        return super().get_context_data(
            charts=services.chart_series(device, range_key),
            range_key=range_key,
            ranges=list(services.CHART_RANGES),
            DEVICE_METRICS_DAYS=settings.DEVICE_METRICS_DAYS,
            locations=device.locations.all()[:40],
            map=maps.for_device(device),
            back_url=_back_to(device),
            now_ymd=timezone.localdate().strftime("%Y%m%d"),
            DEVICE_LOCATION_DAYS=settings.DEVICE_LOCATION_DAYS,
            controls=activity.controls_for(device),
            activity=self._activity(device),
            DEVICE_ACTIVITY_DAYS=settings.DEVICE_ACTIVITY_DAYS,
            **kwargs,
        )

    def _activity(self, device):
        if not device.controls_allowed:
            return None
        today = timezone.localdate()
        try:
            day = date.fromisoformat(self.request.GET.get("day", ""))
        except ValueError:
            day = today
        oldest = today - timedelta(days=settings.DEVICE_ACTIVITY_DAYS)
        day = min(max(day, oldest), today)
        summary = activity.summary(device, day)
        summary["prev"] = day - timedelta(days=1) if day > oldest else None
        summary["next"] = day + timedelta(days=1) if day < today else None
        summary["is_today"] = day == today
        return summary


BLOCK_FIELDS = ("block_adult", "safe_search", "blocked_domains", "allowed_domains")


class DeviceControlsView(StaffViewMixin, AccountScopedMixin, UpdateView):
    """Screen time, websites and blocking for a child's or staff member's computer."""

    permission_required = "devices.change_device"
    model = Device
    form_class = DeviceControlsForm
    template_name = "devices/controls.html"
    context_object_name = "device"

    def get_queryset(self):
        return super().get_queryset().select_related("account", "person")

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            DEVICE_ACTIVITY_DAYS=settings.DEVICE_ACTIVITY_DAYS, live=self._live(), **kwargs
        )

    def _live(self) -> dict:
        """What each control is doing on the computer right now, for the row under it:
        three small aggregate queries, only for a computer that can have controls."""
        device = self.object
        if not device.controls_allowed:
            return {}
        today = timezone.localdate()
        screen = device.screen_time.filter(day=today).aggregate(s=Sum("seconds"))["s"] or 0
        sites = device.web_activity.filter(day=today).values("domain").distinct().count()
        last = device.locations.order_by("-recorded_at").only("source", "recorded_at").first()
        saved = getattr(self._controls(), "updated_at", None)
        try:
            public_ip = bool(device.last_ip) and ipaddress.ip_address(device.last_ip).is_global
        except ValueError:
            public_ip = False
        return {
            "screen_today": screen,
            "sites_today": sites,
            "location_source": last.source if last else "",
            "public_ip": public_ip,
            # Saved after the last check-in: the computer hasn't picked it up yet.
            "pending": bool(saved and device.last_seen_at and saved > device.last_seen_at),
        }

    def _controls(self):
        if not hasattr(self, "_controls_obj"):
            device = self.object
            self._controls_obj = activity.controls_for(device) or DeviceControls(device=device)
        return self._controls_obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self._controls()
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not self.object.can_block:
            # Installed without administrator rights: it can't edit the hosts file. Leave
            # the saved blocking choices alone rather than letting unchecked boxes clear them.
            for name in BLOCK_FIELDS:
                del form.fields[name]
        return form

    def form_valid(self, form):
        if not self.object.controls_allowed:  # the page hides the form; a crafted POST
            messages.error(self.request, self.object.controls_block_reason)
            return redirect("devices:detail", pk=self.object.pk)
        controls = form.save()
        record(self.request, "device.controls", target=self.object.name,
               fields=form.changed_data, screen_time=controls.screen_time,
               web_activity=controls.web_activity, blocking=controls.blocking)  # fmt: skip
        messages.success(
            self.request,
            f"Saved. {self.object.name} picks this up at its next check-in "
            f"(within {self.object.checkin_seconds // 60} minutes).",
        )
        return redirect("devices:detail", pk=self.object.pk)


class DeviceUpdateView(StaffViewMixin, AccountScopedMixin, UpdateView):
    permission_required = "devices.change_device"
    model = Device
    form_class = DeviceForm
    template_name = "devices/form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(self.request, "device.update", target=self.object.name, fields=form.changed_data)
        messages.success(self.request, f"{self.object.name} saved.")
        return response

    def get_success_url(self):
        return reverse("devices:detail", args=[self.object.pk])


class DeviceDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "devices.delete_device"
    model = Device
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("tenants:people")
    extra_context = {"delete_label": "Remove device"}

    def get_success_url(self):
        return _back_to(self.object)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            cancel_url=reverse("devices:detail", args=[self.object.pk]),
            delete_note="The device stops reporting at once, and its location history is "
            "deleted. To watch it again, add it and pair it again.",
            **kwargs,
        )

    def form_valid(self, form):
        record(self.request, "device.delete", target=self.object.name)
        messages.success(self.request, f"{self.object.name} removed.")
        return super().form_valid(form)


# --------------------------------------------------------------------------- places


class PlaceListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "devices.view_device"
    model = Place
    template_name = "devices/places.html"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("people")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        inside = {}
        for s in DevicePlace.objects.filter(place__in=ctx["object_list"], inside=True
                                            ).select_related("device__person"):  # fmt: skip
            inside.setdefault(s.place_id, []).append(s)
        for place in ctx["object_list"]:
            place.here = inside.get(place.pk, [])
            place.map = maps.static_map(place.latitude, place.longitude, place.radius_m)
        return ctx


class _PlaceFormMixin:
    model = Place
    form_class = PlaceForm
    template_name = "devices/place_form.html"
    success_url = reverse_lazy("devices:places")


class PlaceCreateView(StaffViewMixin, AccountScopedMixin, _PlaceFormMixin, CreateView):
    permission_required = "devices.change_device"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(self.request, "place.create", target=self.object.name)
        messages.success(self.request, f"{self.object.name} added.")
        return response


class PlaceUpdateView(StaffViewMixin, AccountScopedMixin, _PlaceFormMixin, UpdateView):
    permission_required = "devices.change_device"

    def form_valid(self, form):
        moved = {"location", "from_device", "radius_m", "people"} & set(form.changed_data)
        response = super().form_valid(form)
        if moved:  # start again from the next fix, quietly
            DevicePlace.objects.filter(place=self.object).delete()
        record(self.request, "place.update", target=self.object.name, fields=form.changed_data)
        messages.success(self.request, f"{self.object.name} saved.")
        return response


class PlaceDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "devices.change_device"
    model = Place
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("devices:places")
    extra_context = {"delete_label": "Delete place"}

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            cancel_url=reverse("devices:places"),
            delete_note="Nobody is told about arriving or leaving here any more.", **kwargs
        )  # fmt: skip

    def form_valid(self, form):
        record(self.request, "place.delete", target=self.object.name)
        messages.success(self.request, f"{self.object.name} deleted.")
        return super().form_valid(form)
