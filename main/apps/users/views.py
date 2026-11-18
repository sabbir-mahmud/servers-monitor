from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import FormView, ListView, TemplateView

from apps.audit.services import record
from apps.core.mixins import StaffViewMixin
from apps.core.utils import client_ip
from apps.tenants import services as tenant_services
from apps.tenants.models import Membership
from apps.tenants.roles import ROLE_HELP, Kind, Role

from . import accounts, throttle, twofactor
from .forms import (
    AcceptInviteForm,
    ForgotPasswordForm,
    InviteForm,
    LoginForm,
    MemberRoleForm,
    ProfileForm,
    SignupForm,
    UserEditForm,
    styled,
)
from .models import Invitation

User = get_user_model()

HOUR = 3600


def _too_many(request, scope: str, limit: int, window: int = HOUR, key: str = "") -> bool:
    return not throttle.allow(scope, f"{client_ip(request)}|{key}", limit, window)


# --------------------------------------------------------------------------- sign in/out


class ThrottledLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    form_class = LoginForm
    redirect_authenticated_user = True
    extra_context = {"allow_signup": settings.ALLOW_SIGNUP}

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if throttle.is_locked(username, client_ip(request)):
            form = self.get_form()
            form.add_error(None, ValidationError("Too many failed attempts. Try again later."))
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        if self.request.method == "POST":
            throttle.record_failure(self.request.POST.get("username", ""), client_ip(self.request))
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        throttle.clear(user.get_username(), client_ip(self.request))
        if twofactor.is_enabled(user):
            # Password was right; the session isn't signed in until the second factor is.
            self.request.session[twofactor.PENDING_KEY] = {
                "uid": user.pk,
                "backend": user.backend,
                "at": timezone.now().timestamp(),
                "next": self.get_redirect_url(),
            }
            return redirect("login_verify")
        return super().form_valid(form)


class LoginVerifyView(View):
    """Second step of sign-in: an authenticator code or a recovery code."""

    template_name = "registration/login_verify.html"

    def _pending(self, request):
        pending = request.session.get(twofactor.PENDING_KEY)
        if not pending or timezone.now().timestamp() - pending["at"] > twofactor.PENDING_SECONDS:
            request.session.pop(twofactor.PENDING_KEY, None)
            return None, None
        return pending, User.objects.filter(pk=pending["uid"], is_active=True).first()

    def get(self, request):
        pending, user = self._pending(request)
        if user is None:
            messages.info(request, "Sign in again: that step timed out.")
            return redirect("login")
        return render(request, self.template_name, {"pending_user": user})

    def post(self, request):
        pending, user = self._pending(request)
        if user is None:
            return redirect("login")
        if not throttle.allow("2fa", str(user.pk), 5, 15 * 60):
            return render(request, self.template_name,
                          {"pending_user": user, "error": "Too many attempts. Wait 15 minutes."})  # fmt: skip
        kind = twofactor.check(user, request.POST.get("code", ""))
        if kind is None:
            record(request, "auth.2fa_failed", target=user.get_username())
            return render(request, self.template_name,
                          {"pending_user": user, "error": "That code isn't right. Try the current one."})  # fmt: skip
        request.session.pop(twofactor.PENDING_KEY, None)
        login(request, user, backend=pending["backend"])
        record(request, "auth.2fa_verified", target=user.get_username(), method=kind)
        if kind == "recovery":
            left = twofactor.remaining_recovery_codes(user)
            messages.warning(
                request,
                f"Recovery code used: {left} left. Generate new ones in My account if that's low.",
            )
        from apps.core.utils import url_is_safe

        nxt = pending.get("next") or ""
        return redirect(nxt if nxt and url_is_safe(request, nxt) else settings.LOGIN_REDIRECT_URL)


class TwoFactorView(LoginRequiredMixin, View):
    """My account → Two-factor: set up, regenerate recovery codes, turn off."""

    template_name = "users/two_factor.html"

    def _render(self, request, **ctx):
        user = request.user
        device = twofactor.device_for(user)
        if device is None or not device.is_active:
            device = twofactor.start_setup(user)
            ctx.setdefault("qr", twofactor.qr_svg(user, device))
            ctx.setdefault("secret", device.secret)
        ctx.update(
            enabled=device.is_active,
            required=twofactor.is_required(user),
            remaining=twofactor.remaining_recovery_codes(user) if device.is_active else 0,
        )
        return render(request, self.template_name, ctx)

    def get(self, request):
        return self._render(request)

    def post(self, request):
        user, action = request.user, request.POST.get("action")
        if action == "confirm":
            device = twofactor.device_for(user)
            codes = twofactor.confirm(device, request.POST.get("code", "")) if device else None
            if codes is None:
                return self._render(
                    request,
                    error="That code isn't right. Check the time on your phone and try the current one.",
                )
            record(request, "user.2fa_enabled", target=user.get_username())
            return self._render(request, codes=codes)
        if action in ("regenerate", "disable"):
            if (
                not user.check_password(request.POST.get("password", ""))
                or twofactor.check(user, request.POST.get("code", "")) is None
            ):
                return self._render(
                    request, error="Enter your password and a current code to do that."
                )
            if action == "regenerate":
                record(request, "user.2fa_codes_regenerated", target=user.get_username())
                return self._render(request, codes=twofactor.issue_recovery_codes(user))
            twofactor.disable(user)
            record(request, "user.2fa_disabled", target=user.get_username())
            messages.success(request, "Two-factor sign-in turned off.")
            return redirect("account_2fa" if twofactor.is_required(user) else "account")
        return redirect("account_2fa")


class LogoutView(auth_views.LogoutView):
    pass


# --------------------------------------------------------------------------- password reset


class ForgotPasswordView(FormView):
    """Always answers the same way, in the same time, whether or not the address has an
    account: the email (if any) goes out from a background thread."""

    template_name = "registration/forgot.html"
    form_class = ForgotPasswordForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("account")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(email_enabled=settings.EMAIL_ENABLED, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        if _too_many(self.request, "reset-ip", 10) or _too_many(
            self.request, "reset-email", 3, key=email
        ):
            form.add_error(None, "Too many reset requests. Try again in an hour.")
            return self.form_invalid(form)
        record(self.request, "auth.password_reset_requested", target=email)
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None and user.has_usable_password() and settings.EMAIL_ENABLED:
            accounts.send_reset_email(self.request, user)
        return render(
            self.request,
            "registration/forgot_done.html",
            {"email": email, "hours": settings.PASSWORD_RESET_TIMEOUT // HOUR},
        )


class ResetPasswordView(auth_views.PasswordResetConfirmView):
    template_name = "registration/reset_confirm.html"
    success_url = reverse_lazy("login")
    post_reset_login = False

    def get_form(self, form_class=None):
        return styled(super().get_form(form_class))

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        record(self.request, "auth.password_reset", target=user.get_username())
        throttle.clear(user.get_username(), client_ip(self.request))
        messages.success(self.request, "Password changed. Sign in with your new password.")
        return response


# --------------------------------------------------------------------------- invitations


class AcceptInviteView(View):
    """Open an invitation. New to this dashboard: choose a username and password. Already
    have a login with that email: sign in as it and join the account with one click."""

    template_name = "registration/invite_accept.html"

    def _invitation(self, token):
        invite = Invitation.find(token)
        return invite if invite is not None and invite.is_usable else None

    def _context(self, request, invite, form=None, **extra):
        existing = User.objects.filter(email__iexact=invite.email).first()
        signed_in = request.user if request.user.is_authenticated else None
        return {
            "invite": invite,
            "form": form or AcceptInviteForm(email=invite.email),
            "existing": existing,
            # Signed in as the invited login: just join. Signed in as someone else: say so.
            "can_join": existing is not None and signed_in is not None and signed_in.pk == existing.pk,
            "logout_first": signed_in is not None and (existing is None or signed_in.pk != existing.pk),
            **extra,
        }  # fmt: skip

    def get(self, request, token):
        invite = self._invitation(token)
        if invite is None:
            return render(request, "registration/invite_invalid.html", status=404)
        return render(request, self.template_name, self._context(request, invite))

    def post(self, request, token):
        if _too_many(request, "invite-ip", 20):
            return render(
                request, "registration/invite_invalid.html", {"throttled": True}, status=429
            )
        invite = self._invitation(token)
        if invite is None:
            return render(request, "registration/invite_invalid.html", status=404)
        ctx = self._context(request, invite)
        if ctx["can_join"]:
            return self._join(request, invite, request.user)
        if ctx["existing"] is not None or ctx["logout_first"]:
            return render(request, self.template_name, ctx)
        form = AcceptInviteForm(request.POST, email=invite.email)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, invite, form))
        data = form.cleaned_data
        with transaction.atomic():
            if User.objects.filter(email__iexact=invite.email).exists():
                return render(request, "registration/invite_invalid.html", status=404)
            user = User.objects.create_user(
                data["username"],
                email=invite.email,
                password=data["password1"],
                first_name=data["first_name"],
                last_name=data["last_name"],
            )
            try:
                tenant_services.join(invite, user)
            except tenant_services.TenantError:
                transaction.set_rollback(True)
                return render(request, "registration/invite_invalid.html", status=404)
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        return self._joined(request, invite, user)

    def _join(self, request, invite, user):
        try:
            tenant_services.join(invite, user)
        except tenant_services.TenantError as exc:
            messages.error(request, str(exc))
            return redirect("home")
        return self._joined(request, invite, user)

    def _joined(self, request, invite, user):
        tenant_services.switch_to(request, invite.account)
        record(request, "user.invite_accepted", target=user.get_username(),
               account=invite.account, role=invite.role)  # fmt: skip
        messages.success(
            request,
            f"Welcome, {user.display_name}. You've joined {invite.account.name} as "
            f"{Role(invite.role).label}.",
        )
        return redirect("home")


class SignupView(FormView):
    """Public sign-up (ALLOW_SIGNUP): a login plus a new, empty account of the chosen type.
    It can't see anything of any other account, so there's nothing to approve."""

    template_name = "registration/signup.html"
    form_class = SignupForm

    def dispatch(self, request, *args, **kwargs):
        if not settings.ALLOW_SIGNUP:
            raise Http404
        if request.user.is_authenticated:
            return redirect("tenants:create")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        kind = self.request.GET.get("kind")
        return {"kind": kind} if kind in Kind.values else {}

    def form_valid(self, form):
        if _too_many(self.request, "signup-ip", 5):
            form.add_error(None, "Too many sign-ups from this network. Try again later.")
            return self.form_invalid(form)
        data = form.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(
                data["username"],
                email=data["email"],
                password=data["password1"],
                first_name=data["first_name"],
                last_name=data["last_name"],
            )
            record(self.request, "user.signup", target=user.get_username(), email=user.email)
            account = tenant_services.create_account(user, user, data["account_name"], data["kind"])
        login(self.request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        tenant_services.switch_to(self.request, account)
        messages.success(self.request, f"Welcome to {settings.SITE_NAME}. {account.name} is ready.")
        return redirect("home")


# --------------------------------------------------------------------------- own account


class AccountView(LoginRequiredMixin, TemplateView):
    template_name = "users/account.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("profile_form", ProfileForm(instance=self.request.user))
        ctx.setdefault("password_form", styled(PasswordChangeForm(self.request.user)))
        # Django focuses "old password" on load, which scrolls the page past everything above.
        ctx["password_form"].fields["old_password"].widget.attrs.pop("autofocus", None)
        role = accounts.role_of(self.request.user)
        ctx["role"] = Role(role).label if role else None
        ctx["role_help"] = ROLE_HELP.get(role, "")
        ctx["has_2fa"] = twofactor.is_enabled(self.request.user)
        ctx["api_tokens"] = self.request.user.api_tokens.filter(revoked_at__isnull=True).count()
        ctx["memberships"] = Membership.objects.filter(user=self.request.user).select_related(
            "account"
        )
        return ctx

    def post(self, request):
        user = request.user
        if "change_password" in request.POST:
            form = styled(PasswordChangeForm(user, request.POST))
            if form.is_valid():
                form.save()
                # Keep this session; every other session of this user is signed out.
                update_session_auth_hash(request, form.user)
                record(request, "auth.password_change", target=user.get_username())
                messages.success(request, "Password changed. Other devices have been signed out.")
                return redirect("account")
            return self.render_to_response(self.get_context_data(password_form=form))
        form = ProfileForm(request.POST, instance=user)
        if form.is_valid():
            old_email = form.original_email
            form.save()
            record(
                request, "user.profile_update", target=user.get_username(), fields=form.changed_data
            )
            if "email" in form.changed_data and old_email and settings.EMAIL_ENABLED:
                # Tell the previous address, so a hijacked account doesn't change hands quietly.
                accounts.send_async(
                    "your email address was changed",
                    "email_changed",
                    {"user": user, "new_email": user.email},
                    old_email,
                )
            messages.success(request, "Profile saved.")
            return redirect("account")
        return self.render_to_response(self.get_context_data(profile_form=form))


# --------------------------------------------------------------------------- members


class UserListView(StaffViewMixin, ListView):
    """Members of the current account, and its open invitations."""

    permission_required = "users.view_user"
    template_name = "users/list.html"
    context_object_name = "members"

    def get_queryset(self):
        return (
            Membership.objects.filter(account=self.request.account)
            .select_related("user")
            .order_by("-user__is_active", "user__username")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            invitations=Invitation.objects.filter(
                account=self.request.account, accepted_at__isnull=True, revoked_at__isnull=True
            )
            .select_related("invited_by")
            .order_by("-created_at")[:50],
            now=timezone.now(),
        )
        return ctx


def _invite_form_kwargs(request) -> dict:
    return {
        "account": request.account,
        "actor_role": accounts.role_of(request.user),
        "actor_is_superuser": request.user.is_superuser,
    }


class InviteView(StaffViewMixin, FormView):
    permission_required = "users.add_user"
    template_name = "users/invite.html"
    form_class = InviteForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), **_invite_form_kwargs(self.request)}

    def form_valid(self, form):
        email, role = form.cleaned_data["email"], form.cleaned_data["role"]
        account = self.request.account
        # One live invitation per address and account: re-inviting replaces the old link.
        Invitation.objects.filter(
            account=account, email__iexact=email, accepted_at__isnull=True, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        invite, raw = Invitation.issue(account, email, role, self.request.user)
        record(self.request, "user.invite", target=email, role=role)
        if settings.EMAIL_ENABLED:
            accounts.send_invite_email(self.request, invite, raw)
        return render(
            self.request,
            "users/invite_created.html",
            {
                "invite": invite,
                "link": accounts.invite_link(self.request, raw),
                "email_enabled": settings.EMAIL_ENABLED,
            },
        )


class InviteRevokeView(StaffViewMixin, View):
    permission_required = "users.add_user"
    http_method_names = ["post"]

    def post(self, request, pk):
        invite = get_object_or_404(
            Invitation, pk=pk, account=request.account, accepted_at__isnull=True
        )
        if invite.revoked_at is None:
            invite.revoked_at = timezone.now()
            invite.save(update_fields=["revoked_at"])
            record(request, "user.invite_revoked", target=invite.email)
            messages.success(request, f"Invitation for {invite.email} revoked.")
        return redirect("users:list")


class UserDetailView(StaffViewMixin, View):
    """One member: their role here, removing them, and, when this account is the only one
    they belong to, their login (profile, password reset, two-factor, deactivate)."""

    permission_required = "users.change_user"
    template_name = "users/detail.html"

    def _membership(self, request, pk):
        return get_object_or_404(
            Membership.objects.select_related("user", "account"),
            account=request.account,
            user_id=pk,
        )

    def _context(self, request, membership, form=None, **extra):
        person = membership.user
        mine = accounts.role_of(request.user)
        return {
            "person": person,
            "membership": membership,
            "role_form": MemberRoleForm(
                initial={"role": membership.role}, **_invite_form_kwargs(request)
            ),
            "form": form or UserEditForm(instance=person),
            "is_self": person.pk == request.user.pk,
            "is_last_owner": accounts.is_last_owner(membership),
            "can_change_role": person.pk != request.user.pk
            and (membership.role != Role.OWNER or mine == Role.OWNER or request.user.is_superuser),
            "can_manage": accounts.can_manage_login(request.user, person, request.account),
            "other_accounts": Membership.objects.filter(user=person)
            .exclude(account=request.account)
            .count(),
            "email_enabled": settings.EMAIL_ENABLED,
            "role_help": {r: ROLE_HELP[r] for r in request.account.roles},
            "has_2fa": twofactor.is_enabled(person),
            **extra,
        }

    def get(self, request, pk):
        membership = self._membership(request, pk)
        return render(request, self.template_name, self._context(request, membership))

    def post(self, request, pk):
        membership = self._membership(request, pk)
        person = membership.user
        action = request.POST.get("action", "save")
        try:
            if action == "role":
                form = MemberRoleForm(request.POST, **_invite_form_kwargs(request))
                if not form.is_valid():
                    raise accounts.AccountError("Choose one of the roles listed.")
                old = membership.role
                accounts.set_role(membership, form.cleaned_data["role"], actor=request.user)
                record(request, "user.role", target=person.get_username(),
                       role_from=old, role_to=membership.role)  # fmt: skip
                messages.success(request, f"{person.display_name} is now {membership.role_label}.")
            elif action == "remove":
                accounts.remove_member(membership, actor=request.user)
                record(request, "user.remove", target=person.get_username(), role=membership.role)
                messages.success(
                    request,
                    f"{person.display_name} no longer has access to {request.account.name}.",
                )
                return redirect("users:list")
            elif action in (
                "save",
                "deactivate",
                "activate",
                "reset_2fa",
                "reset_link",
                "reset_email",
            ):
                return self._login_action(request, membership, action)
            else:
                raise Http404
        except accounts.AccountError as exc:
            messages.error(request, str(exc))
        return redirect("users:detail", pk=person.pk)

    def _login_action(self, request, membership, action):
        person = membership.user
        accounts.check_can_manage(request.user, person)
        if not accounts.can_manage_login(request.user, person, request.account):
            raise accounts.AccountError(
                f"{person.display_name} also belongs to other accounts, so only they (or a "
                "superuser) can change their login."
            )
        if action == "save":
            form = UserEditForm(request.POST, instance=person)
            if not form.is_valid():
                return render(request, self.template_name, self._context(request, membership, form))
            form.save()
            record(request, "user.update", target=person.get_username(), fields=form.changed_data)
            messages.success(request, f"{person.display_name} saved.")
        elif action in ("deactivate", "activate"):
            accounts.set_active(person, action == "activate", actor=request.user)
            record(request, f"user.{action}", target=person.get_username())
            messages.success(
                request,
                f"{person.display_name} {'reactivated' if action == 'activate' else 'deactivated and signed out everywhere'}.",
            )
        elif action == "reset_2fa":
            twofactor.disable(person)
            record(request, "user.2fa_reset", target=person.get_username())
            messages.success(
                request,
                f"Two-factor removed for {person.display_name}. They set it up again at next sign-in.",
            )
        elif action == "reset_link":
            link = accounts.reset_link(request, person)
            record(request, "user.reset_link", target=person.get_username())
            return render(
                request, self.template_name, self._context(request, membership, reset_link=link)
            )
        elif action == "reset_email":
            if not (settings.EMAIL_ENABLED and person.email):
                raise accounts.AccountError(
                    "Email isn't configured, or this person has no address."
                )
            accounts.send_reset_email(request, person)
            record(request, "user.reset_email", target=person.get_username())
            messages.success(request, f"Reset email sent to {person.email}.")
        return redirect("users:detail", pk=person.pk)
