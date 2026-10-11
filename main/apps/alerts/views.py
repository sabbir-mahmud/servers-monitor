from datetime import timedelta

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.audit.services import record
from apps.core.mixins import HtmxTemplateMixin, StaffViewMixin

from . import delivery, maintenance
from .forms import ChannelForm, MaintenanceForm
from .models import Alert, Channel, ChannelKind, Delivery, MaintenanceWindow


class AlertListView(StaffViewMixin, HtmxTemplateMixin, ListView):
    permission_required = "alerts.view_alert"
    template_name = "alerts/list.html"
    htmx_partial = "rows"
    paginate_by = 50

    def get_queryset(self):
        qs = Alert.objects.select_related("server", "monitor", "acknowledged_by")
        state = self.request.GET.get("state", "open")
        if state == "open":
            qs = qs.filter(resolved_at__isnull=True)
        elif state == "resolved":
            qs = qs.filter(resolved_at__isnull=False)
        if q := self.request.GET.get("q", "").strip():
            qs = qs.filter(Q(server__name__icontains=q) | Q(message__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["state"] = self.request.GET.get("state", "open")
        cov = maintenance.coverage()
        for alert in ctx["object_list"]:
            alert.muted = alert.is_open and cov.covers_alert(alert)
        return ctx


class AlertAcknowledgeView(StaffViewMixin, View):
    permission_required = "alerts.change_alert"
    http_method_names = ["post"]

    def post(self, request, pk):
        alert = get_object_or_404(Alert.objects.select_related("server"), pk=pk)
        if alert.acknowledged_at is None:
            alert.acknowledged_by, alert.acknowledged_at = request.user, timezone.now()
            alert.save(update_fields=["acknowledged_by", "acknowledged_at"])
            record(request, "alert.acknowledge", target=str(alert), server=alert.server)
            messages.success(request, "Alert acknowledged.")
        return redirect(request.POST.get("next") or "alerts:list")


# --------------------------------------------------------------------------- notifications


class ChannelListView(StaffViewMixin, ListView):
    permission_required = "alerts.view_channel"
    template_name = "alerts/channels.html"
    context_object_name = "channels"
    model = Channel

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["deliveries"] = Delivery.objects.select_related("channel", "alert")[:30]
        return ctx


class _ChannelFormMixin:
    template_name = "alerts/channel_form.html"
    form_class = ChannelForm
    model = Channel
    success_url = reverse_lazy("alerts:channels")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kind = self.request.GET.get("kind") or self.request.POST.get("kind")
        kwargs["kind"] = kind if kind in ChannelKind.values else None
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["kind"] = ctx["form"].kind
        ctx["kinds"] = ChannelKind.choices
        return ctx


class ChannelCreateView(StaffViewMixin, _ChannelFormMixin, CreateView):
    permission_required = "alerts.add_channel"

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        record(self.request, "channel.create", target=self.object.name, kind=self.object.kind)
        messages.success(self.request, f"{self.object.name} added. Send a test to check it.")
        return response


class ChannelUpdateView(StaffViewMixin, _ChannelFormMixin, UpdateView):
    permission_required = "alerts.change_channel"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(self.request, "channel.update", target=self.object.name, fields=form.changed_data)
        messages.success(self.request, "Channel saved.")
        return response


class ChannelDeleteView(StaffViewMixin, DeleteView):
    permission_required = "alerts.delete_channel"
    template_name = "confirm_delete.html"
    model = Channel
    success_url = reverse_lazy("alerts:channels")
    extra_context = {
        "delete_note": "Alerts stop going to this channel. Its delivery history is removed too.",
        "delete_label": "Delete channel",
    }

    def form_valid(self, form):
        record(self.request, "channel.delete", target=self.object.name)
        return super().form_valid(form)


class ChannelTestView(StaffViewMixin, View):
    permission_required = "alerts.change_channel"
    http_method_names = ["post"]

    def post(self, request, pk):
        channel = get_object_or_404(Channel, pk=pk)
        result = delivery.send_test(channel, request.user)
        if result.ok:
            messages.success(request, f"Test message sent to {channel.name}.")
        else:
            messages.error(request, f"{channel.name} failed: {result.error}")
        return redirect("alerts:channels")


class TelegramChatsView(StaffViewMixin, View):
    """Find the chat id: list chats that messaged the bot (getUpdates, outbound only)."""

    permission_required = "alerts.add_channel"
    http_method_names = ["post"]

    def post(self, request):
        token = request.POST.get("telegram_token", "").strip()
        if not token and (pk := request.POST.get("channel")):
            token = get_object_or_404(Channel, pk=pk).telegram_token
        error, chats = "", []
        if not token:
            error = "Paste the bot token first."
        else:
            try:
                chats = delivery.telegram_chats(token)
            except delivery.SendError as exc:
                error = str(exc)
        return render(request, "alerts/_telegram_chats.html", {"chats": chats, "error": error})


# --------------------------------------------------------------------------- maintenance


class MaintenanceListView(StaffViewMixin, ListView):
    permission_required = "alerts.view_maintenancewindow"
    template_name = "alerts/maintenance.html"
    context_object_name = "windows"

    def get_queryset(self):
        return MaintenanceWindow.objects.prefetch_related("servers", "monitors").select_related(
            "created_by"
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        windows = list(ctx["windows"])
        ctx.update(
            active=[w for w in windows if w.starts_at <= now < w.ends_at],
            upcoming=sorted((w for w in windows if w.starts_at > now), key=lambda w: w.starts_at),
            past=[w for w in windows if w.ends_at <= now][:20],
        )
        return ctx


class MaintenanceCreateView(StaffViewMixin, CreateView):
    permission_required = "alerts.add_maintenancewindow"
    template_name = "alerts/maintenance_form.html"
    form_class = MaintenanceForm
    success_url = reverse_lazy("alerts:maintenance")

    def get_initial(self):
        now = timezone.localtime().replace(second=0, microsecond=0)
        initial = {"starts_at": now, "ends_at": now + timedelta(hours=1)}
        if server := self.request.GET.get("server"):
            initial["servers"] = [server]
        if monitor := self.request.GET.get("monitor"):
            initial["monitors"] = [monitor]
        return initial

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        w = self.object
        record(
            self.request,
            "maintenance.create",
            target=w.title,
            starts=w.starts_at.isoformat(),
            ends=w.ends_at.isoformat(),
            everything=w.all_targets,
            servers=[s.name for s in w.servers.all()],
            apps=[m.name for m in w.monitors.all()],
        )
        messages.success(self.request, f"Maintenance window “{w.title}” scheduled.")
        return response


class MaintenanceEndView(StaffViewMixin, View):
    permission_required = "alerts.change_maintenancewindow"
    http_method_names = ["post"]

    def post(self, request, pk):
        window = get_object_or_404(MaintenanceWindow, pk=pk)
        maintenance.end_now(window)
        record(request, "maintenance.end", target=window.title)
        messages.success(
            request, f"“{window.title}” ended. Held alerts that are still open are sent now."
        )
        return redirect(request.POST.get("next") or "alerts:maintenance")


class MaintenanceDeleteView(StaffViewMixin, DeleteView):
    permission_required = "alerts.delete_maintenancewindow"
    template_name = "confirm_delete.html"
    model = MaintenanceWindow
    success_url = reverse_lazy("alerts:maintenance")
    extra_context = {
        "delete_note": "Removes this window from the schedule.",
        "delete_label": "Delete window",
    }

    def form_valid(self, form):
        record(self.request, "maintenance.delete", target=self.object.title)
        return super().form_valid(form)


class QuickMuteView(StaffViewMixin, View):
    """“Mute for 1 h” from a server or application page."""

    permission_required = "alerts.add_maintenancewindow"
    http_method_names = ["post"]

    def post(self, request):
        from apps.fleet.models import Server
        from apps.monitors.models import Monitor

        try:
            minutes = max(5, min(int(request.POST.get("minutes", 60)), 7 * 24 * 60))
        except ValueError:
            minutes = 60
        server = monitor = None
        if pk := request.POST.get("server"):
            server = get_object_or_404(Server, pk=pk)
        elif pk := request.POST.get("monitor"):
            monitor = get_object_or_404(Monitor, pk=pk)
        else:
            return redirect("alerts:maintenance")
        window = maintenance.quick_mute(request.user, minutes, server=server, monitor=monitor)
        record(request, "maintenance.create", target=window.title, minutes=minutes, server=server)
        messages.success(
            request,
            f"{(server or monitor).name} muted until {timezone.localtime(window.ends_at):%H:%M}. "
            "Alerts are still recorded; notifications wait.",
        )
        return redirect(request.POST.get("next") or "alerts:maintenance")
