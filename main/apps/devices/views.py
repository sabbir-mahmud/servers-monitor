from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView

from apps.audit.services import record
from apps.core import crypto
from apps.core.mixins import StaffViewMixin
from apps.tenants.models import Person
from apps.tenants.scoping import AccountScopedMixin, scoped

from . import connect, maps, services
from .forms import DeviceForm
from .models import COMPUTERS, Device

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


def _qr(server: str, code: str) -> str:
    """What the NexGuard phone app scans: where to pair and with which code."""
    from urllib.parse import urlencode

    import segno

    uri = "nexguard://pair?" + urlencode({"server": server, "code": code.replace("-", "")})
    return segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#1d1d1f",
                                                 light="#ffffff")  # fmt: skip


class DeviceDetailView(StaffViewMixin, AccountScopedMixin, DetailView):
    permission_required = "devices.view_device"
    model = Device
    template_name = "devices/detail.html"
    context_object_name = "device"

    def get_queryset(self):
        return super().get_queryset().select_related("person")

    def get_context_data(self, **kwargs):
        device = self.object
        return super().get_context_data(
            locations=device.locations.all()[:40],
            map=maps.for_device(device),
            back_url=_back_to(device),
            now_ymd=timezone.localdate().strftime("%Y%m%d"),
            DEVICE_LOCATION_DAYS=settings.DEVICE_LOCATION_DAYS,
            **kwargs,
        )


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
