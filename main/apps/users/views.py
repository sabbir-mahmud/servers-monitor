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

from . import accounts, throttle
from .forms import (
    AcceptInviteForm,
    ForgotPasswordForm,
    InviteForm,
    LoginForm,
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
        throttle.clear(form.get_user().get_username(), client_ip(self.request))
        return super().form_valid(form)


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
    template_name = "registration/invite_accept.html"

    def _invitation(self, token):
        invite = Invitation.find(token)
        return invite if invite is not None and invite.is_usable else None

    def get(self, request, token):
        logout_first = request.user.is_authenticated
        invite = self._invitation(token)
        if invite is None:
            return render(request, "registration/invite_invalid.html", status=404)
        form = AcceptInviteForm(email=invite.email)
        return render(
            request,
            self.template_name,
            {"form": form, "invite": invite, "logout_first": logout_first},
        )

    def post(self, request, token):
        if _too_many(request, "invite-ip", 20):
            return render(
                request, "registration/invite_invalid.html", {"throttled": True}, status=429
            )
        invite = self._invitation(token)
        if invite is None:
            return render(request, "registration/invite_invalid.html", status=404)
        form = AcceptInviteForm(request.POST, email=invite.email)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "invite": invite})
        data = form.cleaned_data
        with transaction.atomic():
            # Re-check inside the transaction: two tabs must not both use one invite.
            invite = Invitation.objects.select_for_update().get(pk=invite.pk)
            if not invite.is_usable or User.objects.filter(email__iexact=invite.email).exists():
                return render(request, "registration/invite_invalid.html", status=404)
            user = User.objects.create_user(
                data["username"],
                email=invite.email,
                password=data["password1"],
                first_name=data["first_name"],
                last_name=data["last_name"],
            )
            accounts.set_role(user, invite.role)
            invite.accepted_at, invite.accepted_by = timezone.now(), user
            invite.save(update_fields=["accepted_at", "accepted_by"])
        record(request, "user.invite_accepted", target=user.get_username(), role=invite.role)
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        messages.success(
            request, f"Welcome, {user.display_name}. You're signed in as {invite.role}."
        )
        return redirect("fleet:dashboard")


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = SignupForm

    def dispatch(self, request, *args, **kwargs):
        if not settings.ALLOW_SIGNUP:
            raise Http404
        if request.user.is_authenticated:
            return redirect("fleet:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        if _too_many(self.request, "signup-ip", 5):
            form.add_error(None, "Too many sign-ups from this network. Try again later.")
            return self.form_invalid(form)
        data = form.cleaned_data
        user = User.objects.create_user(
            data["username"],
            email=data["email"],
            password=data["password1"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            is_active=False,
            pending_approval=True,
        )
        record(self.request, "user.signup", target=user.get_username(), email=user.email)
        return render(self.request, "registration/signup_done.html", {"new_user": user})


# --------------------------------------------------------------------------- own account


class AccountView(LoginRequiredMixin, TemplateView):
    template_name = "users/account.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("profile_form", ProfileForm(instance=self.request.user))
        ctx.setdefault("password_form", styled(PasswordChangeForm(self.request.user)))
        ctx["role"] = accounts.role_of(self.request.user)
        ctx["role_help"] = accounts.ROLE_HELP.get(ctx["role"], "")
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


# --------------------------------------------------------------------------- management


class UserListView(StaffViewMixin, ListView):
    permission_required = "users.view_user"
    template_name = "users/list.html"
    context_object_name = "people"

    def get_queryset(self):
        return User.objects.prefetch_related("groups").order_by("-is_active", "username")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        people = list(ctx["people"])
        for u in people:
            u.role = accounts.role_of(u)
        ctx.update(
            pending=[u for u in people if u.pending_approval],
            people=[u for u in people if not u.pending_approval],
            invitations=Invitation.objects.filter(accepted_at__isnull=True, revoked_at__isnull=True)
            .select_related("invited_by")
            .order_by("-created_at")[:50],
            now=timezone.now(),
        )
        return ctx


class InviteView(StaffViewMixin, FormView):
    permission_required = "users.add_user"
    template_name = "users/invite.html"
    form_class = InviteForm

    def form_valid(self, form):
        email, role = form.cleaned_data["email"], form.cleaned_data["role"]
        # One live invitation per address: re-inviting replaces the old link.
        Invitation.objects.filter(
            email__iexact=email, accepted_at__isnull=True, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        invite, raw = Invitation.issue(email, role, self.request.user)
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
        invite = get_object_or_404(Invitation, pk=pk, accepted_at__isnull=True)
        if invite.revoked_at is None:
            invite.revoked_at = timezone.now()
            invite.save(update_fields=["revoked_at"])
            record(request, "user.invite_revoked", target=invite.email)
            messages.success(request, f"Invitation for {invite.email} revoked.")
        return redirect("users:list")


class UserDetailView(StaffViewMixin, View):
    permission_required = "users.change_user"
    template_name = "users/detail.html"

    def _context(self, request, person, form=None, **extra):
        person.role = accounts.role_of(person)
        return {
            "person": person,
            "form": form or UserEditForm(instance=person, initial={"role": person.role or ""}),
            "is_self": person.pk == request.user.pk,
            "is_last_admin": accounts.is_last_admin(person),
            "email_enabled": settings.EMAIL_ENABLED,
            "role_help": accounts.ROLE_HELP,
            "can_manage": not (person.is_superuser and not request.user.is_superuser),
            **extra,
        }

    def get(self, request, pk):
        person = get_object_or_404(User, pk=pk)
        return render(request, self.template_name, self._context(request, person))

    def post(self, request, pk):
        person = get_object_or_404(User, pk=pk)
        action = request.POST.get("action", "save")
        try:
            accounts.check_can_manage(request.user, person)
            if action == "save":
                form = UserEditForm(request.POST, instance=person)
                if not form.is_valid():
                    return render(request, self.template_name, self._context(request, person, form))
                new_role = form.cleaned_data["role"] or None
                old_role = accounts.role_of(person)
                with transaction.atomic():
                    if new_role != old_role:
                        accounts.set_role(person, new_role, actor=request.user)
                    form.save()
                record(request, "user.update", target=person.get_username(),
                       fields=form.changed_data, role_from=old_role, role_to=new_role)  # fmt: skip
                messages.success(request, f"{person.display_name} saved.")
            elif action == "approve":
                role = request.POST.get("role") or "Viewer"
                with transaction.atomic():
                    accounts.set_role(person, role)
                    accounts.set_active(person, True, actor=request.user)
                record(request, "user.approve", target=person.get_username(), role=role)
                messages.success(request, f"{person.display_name} approved as {role}.")
            elif action in ("deactivate", "activate"):
                accounts.set_active(person, action == "activate", actor=request.user)
                record(request, f"user.{action}", target=person.get_username())
                messages.success(
                    request,
                    f"{person.display_name} {'reactivated' if action == 'activate' else 'deactivated and signed out everywhere'}.",
                )
            elif action == "reset_link":
                link = accounts.reset_link(request, person)
                record(request, "user.reset_link", target=person.get_username())
                return render(
                    request, self.template_name, self._context(request, person, reset_link=link)
                )
            elif action == "reset_email":
                if not (settings.EMAIL_ENABLED and person.email):
                    raise accounts.AccountError(
                        "Email isn't configured, or this person has no address."
                    )
                accounts.send_reset_email(request, person)
                record(request, "user.reset_email", target=person.get_username())
                messages.success(request, f"Reset email sent to {person.email}.")
            else:
                raise Http404
        except accounts.AccountError as exc:
            messages.error(request, str(exc))
        return redirect("users:detail", pk=person.pk)
