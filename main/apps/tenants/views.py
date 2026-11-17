from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

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


class PersonListView(StaffViewMixin, AccountScopedMixin, ListView):
    permission_required = "tenants.view_person"
    template_name = "tenants/people.html"
    model = Person
    context_object_name = "people"


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
            delete_note="Removes them from this account. Their devices stay, unassigned.",
            **kwargs,
        )

    def form_valid(self, form):
        record(self.request, "person.delete", target=self.object.name)
        messages.success(self.request, f"{self.object.name} removed.")
        return super().form_valid(form)
