from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin
from apps.users import accounts

from . import services
from .forms import AccountSettingsForm, PersonForm
from .models import Membership, Person
from .roles import KIND_HELP, Kind
from .scoping import AccountScopedMixin


class HomeView(LoginRequiredMixin, View):
    """Where signing in lands: the account type's natural first page."""

    def get(self, request):
        account = request.account
        if account.has("servers") and request.user.has_perm("fleet.view_server"):
            return redirect("fleet:dashboard")
        if account.has("people") and request.user.has_perm("tenants.view_person"):
            return redirect("tenants:people")
        return redirect("tenants:settings")


def can_create_account(user) -> bool:
    return settings.ALLOW_SIGNUP or user.is_superuser


class AccountCreateView(LoginRequiredMixin, View):
    """Create another account (a developer who's also a parent), or a first one for a
    login that belongs to none (removed from its last account)."""

    template_name = "tenants/create.html"

    def _render(self, request, form=None, status=200):
        from apps.users.forms import AccountKindForm

        kind = request.GET.get("kind")
        return render(
            request,
            self.template_name,
            {
                "form": form
                or AccountKindForm(initial={"kind": kind} if kind in Kind.values else None),
                "allowed": can_create_account(request.user),
                "has_accounts": bool(request.memberships),
                "kind_help": KIND_HELP,
            },
            status=status,
        )

    def get(self, request):
        return self._render(request)

    def post(self, request):
        from apps.users.forms import AccountKindForm

        if not can_create_account(request.user):
            return self._render(request, status=403)
        form = AccountKindForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)
        account = services.create_account(
            request, request.user, form.cleaned_data["account_name"], form.cleaned_data["kind"]
        )
        services.switch_to(request, account)
        messages.success(request, f"{account.name} created. You're its owner.")
        return redirect("home")


class SwitchAccountView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request):
        membership = get_object_or_404(
            Membership.objects.select_related("account"),
            user=request.user,
            account_id=request.POST.get("account"),
        )
        services.switch_to(request, membership.account)
        return redirect("home")


class AccountSettingsView(LoginRequiredMixin, View):
    """The current account: its name and type, who owns it, leaving it."""

    template_name = "tenants/settings.html"

    def _context(self, request, form=None):
        account = request.account
        can_edit = request.user.has_perm("tenants.change_account")
        return {
            "account": account,
            "form": form or (AccountSettingsForm(instance=account) if can_edit else None),
            "can_edit": can_edit,
            "owners": accounts.owners(account).select_related("user"),
            "kind_help": KIND_HELP.get(account.kind, ""),
            "is_last_owner": accounts.is_last_owner(request.membership),
            "member_count": account.memberships.count(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        action = request.POST.get("action", "save")
        if action == "leave":
            name = request.account.name
            try:
                accounts.remove_member(request.membership, actor=request.user)
            except accounts.AccountError as exc:
                messages.error(request, str(exc))
                return redirect("tenants:settings")
            record(request, "account.leave", target=name)
            messages.success(request, f"You left {name}.")
            return redirect("home")
        if not request.user.has_perm("tenants.change_account"):
            raise Http404
        form = AccountSettingsForm(request.POST, instance=request.account)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, form))
        form.save()
        record(request, "account.update", target=request.account.name, fields=form.changed_data)
        messages.success(request, "Account saved.")
        return redirect("tenants:settings")


# --------------------------------------------------------------------------- people


def _with_devices(request, queryset):
    """Each person's devices, one query for the whole page (none without the feature)."""
    from apps.devices.models import Device

    if not request.user.has_perm("devices.view_device"):
        return queryset
    return queryset.prefetch_related(
        Prefetch("devices", queryset=Device.objects.filter(account=request.account))
    )


class PersonListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "tenants.view_person"
    template_name = "tenants/people.html"
    model = Person
    context_object_name = "people"

    def get_queryset(self):
        return _with_devices(self.request, super().get_queryset())

    def get_context_data(self, **kwargs):
        from apps.devices.models import COMPUTERS, PHONES, Device, Platform, summarize

        ctx = super().get_context_data(**kwargs)
        show_devices = self.request.user.has_perm("devices.view_device")
        unassigned = []
        if show_devices:
            unassigned = list(Device.objects.filter(account=self.request.account, person=None))
        people = list(ctx["people"])
        for person in people:
            person.summary = summarize(person.devices.all() if show_devices else [])
        everything = [d for p in people for d in p.summary["devices"]] + unassigned

        # Search and filter in Python: a family has a handful of people, a company a few
        # hundred, and the states depend on the current time anyway.
        query = self.request.GET.get("q", "").strip()
        show = self.request.GET.get("show", "all")
        tests = {
            "all": lambda p: True,
            "attention": lambda p: bool(p.summary["attention"]),
            "online": lambda p: p.summary["online"] > 0,
            "offline": lambda p: any(d.state == "offline" for d in p.summary["devices"]),
            "none": lambda p: not p.summary["devices"],
        }
        if show not in tests or not show_devices:
            show = "all"
        matching = [p for p in people if not query or query.lower() in p.name.lower()]
        filters = [
            {"key": key, "label": label, "count": sum(1 for p in matching if tests[key](p))}
            for key, label in (
                ("all", "All"),
                ("attention", "Needs a look"),
                ("online", "Online"),
                ("offline", "Offline"),
                ("none", "No devices"),
            )  # fmt: skip
        ]
        shown = [p for p in matching if tests[show](p)]
        # Whoever needs a look comes first; otherwise alphabetical.
        shown.sort(key=lambda p: (not p.summary["attention"], p.name.lower()))
        ctx.update(
            people=people,
            shown=shown,
            query=query,
            show=show,
            filters=filters if show_devices else [],
            unassigned=unassigned,
            show_devices=show_devices,
            family=self.request.account.kind == "family",
            totals=summarize(everything),
            # Quick "add a device" buttons on a child with none yet.
            platforms=[Platform(p) for p in (*PHONES, *COMPUTERS)],
        )
        return ctx


class PersonDetailView(StaffViewMixin, AccountScopedMixin, DetailView):
    """One child (or staff member) and their devices."""

    permission_required = "tenants.view_person"
    template_name = "tenants/person_detail.html"
    model = Person
    context_object_name = "person"

    def get_queryset(self):
        return _with_devices(self.request, super().get_queryset())

    def get_context_data(self, **kwargs):
        from apps.devices import maps
        from apps.devices.models import COMPUTERS, PHONES, DeviceLocation, Platform, summarize

        person = self.object
        show_devices = self.request.user.has_perm("devices.view_device")
        summary = summarize(person.devices.all() if show_devices else [])
        located = summary["last_location"]
        recent = []
        if show_devices:
            recent = list(
                DeviceLocation.objects.filter(
                    device__person=person, device__account=self.request.account
                ).select_related("device")[:6]
            )
        return super().get_context_data(
            show_devices=show_devices,
            summary=summary,
            map=maps.for_device(located) if located else None,
            map_device=located,
            recent_places=recent,
            has_phone=any(d.is_phone for d in summary["devices"]),
            now_ymd=timezone.localdate().strftime("%Y%m%d"),
            computers=[Platform(p) for p in COMPUTERS],
            phones=[Platform(p) for p in PHONES],
            **kwargs,
        )


class _PersonFormMixin(AccountScopedMixin):
    model = Person
    form_class = PersonForm
    template_name = "tenants/person_form.html"
    success_url = reverse_lazy("tenants:people")


class PersonCreateView(StaffViewMixin, _PersonFormMixin, CreateView):
    permission_required = "tenants.add_person"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(self.request, "person.create", target=self.object.name, kind=self.object.kind)
        messages.success(self.request, f"{self.object.name} added.")
        return response


class PersonUpdateView(StaffViewMixin, _PersonFormMixin, UpdateView):
    permission_required = "tenants.change_person"

    def form_valid(self, form):
        response = super().form_valid(form)
        record(self.request, "person.update", target=self.object.name, fields=form.changed_data)
        messages.success(self.request, f"{self.object.name} saved.")
        return response


class PersonDeleteView(StaffViewMixin, AccountScopedMixin, DeleteView):
    permission_required = "tenants.delete_person"
    template_name = "confirm_delete.html"
    model = Person
    success_url = reverse_lazy("tenants:people")
    extra_context = {"delete_label": "Remove"}

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            cancel_url=reverse("tenants:people"),
            delete_note="Removes them from this account. Their devices stay, unassigned "
            "(remove those separately to stop them reporting).",
            **kwargs,
        )

    def form_valid(self, form):
        record(self.request, "person.delete", target=self.object.name)
        messages.success(self.request, f"{self.object.name} removed.")
        return super().form_valid(form)
