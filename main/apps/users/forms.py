from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.validators import UnicodeUsernameValidator

from apps.tenants.models import Membership
from apps.tenants.roles import KIND_HELP, ROLE_HELP, Kind, Role

from .models import Invitation

User = get_user_model()


def _style(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, forms.RadioSelect):
            continue
        elif isinstance(w, forms.Select):
            w.attrs.setdefault("class", "form-select")
        else:
            w.attrs.setdefault("class", "form-control")


def _email_taken(email: str, exclude_pk=None) -> bool:
    qs = User.objects.filter(email__iexact=email.strip())
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Username or email"

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError:
            # Only after the right password: say the account is waiting, not "wrong
            # password". Without the password this reveals nothing.
            username, password = (
                self.cleaned_data.get("username"),
                self.cleaned_data.get("password"),
            )
            if username and password:
                from django.db.models import Q

                user = User.objects.filter(
                    Q(username__iexact=username) | Q(email__iexact=username), pending_approval=True
                ).first()
                if user is not None and user.check_password(password):
                    raise forms.ValidationError(
                        "Your account is waiting for an administrator to approve it.",
                        code="pending",
                    ) from None
            raise


class _NewAccountForm(forms.Form):
    """Username, names and password with Django's validators, for invite and sign-up."""

    username = forms.CharField(
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        help_text="Letters, digits and @ . + - _ only.",
    )
    first_name = forms.CharField(max_length=150, required=False, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="At least 10 characters, not a common password, not all numbers.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, email: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.email = email
        _style(self)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords don't match.")
        elif p1:
            probe = User(
                username=data.get("username", ""),
                email=self.email or data.get("email", ""),
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
            )
            try:
                password_validation.validate_password(p1, probe)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data


class AcceptInviteForm(_NewAccountForm):
    pass


class AccountKindForm(forms.Form):
    """What kind of account, and its name: sign-up and "create another account"."""

    kind = forms.ChoiceField(
        choices=Kind.choices, widget=forms.RadioSelect, label="Who is it for?", initial=Kind.FAMILY
    )
    account_name = forms.CharField(
        max_length=100, label="Account name",
        help_text="e.g. The Rahmans, Acme Ltd or your name. You can change it later.",
    )  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind_help = KIND_HELP
        _style(self)


class SignupForm(AccountKindForm, _NewAccountForm):
    email = forms.EmailField()
    field_order = ["kind", "account_name", "email", "username", "first_name", "last_name",
                   "password1", "password2"]  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(self.field_order)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if _email_taken(email):
            # Naming the clash costs little (sign-up is throttled) and saves a confused
            # person a dead end. Password reset never does this.
            raise forms.ValidationError(
                "An account with this email already exists. Sign in instead."
            )
        return email


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


def role_choices(account, actor_role: str | None, *, actor_is_superuser=False):
    """Roles this account type has; only owners hand out "owner"."""
    can_grant_owner = actor_role == Role.OWNER or actor_is_superuser
    return [(r.value, r.label) for r in account.roles if r != Role.OWNER or can_grant_owner]


class InviteForm(forms.Form):
    """Invite someone into the current account. They may already have a login (a parent
    who is also a developer): accepting adds a membership to their existing account."""

    email = forms.EmailField()
    role = forms.ChoiceField(widget=forms.RadioSelect)

    def __init__(self, *args, account, actor_role=None, actor_is_superuser=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        choices = role_choices(account, actor_role, actor_is_superuser=actor_is_superuser)
        self.fields["role"].choices = choices
        default = Role.VIEWER if Role.VIEWER in account.roles else choices[-1][0]
        self.fields["role"].initial = default
        self.fields["email"].widget.attrs["placeholder"] = (
            "partner@example.com" if account.kind == Kind.FAMILY else "teammate@company.com"
        )
        self.role_help = {r: ROLE_HELP[r] for r, _ in choices}
        _style(self)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if Membership.objects.filter(account=self.account, user__email__iexact=email).exists():
            raise forms.ValidationError(f"They're already a member of {self.account.name}.")
        return email


class MemberRoleForm(forms.Form):
    role = forms.ChoiceField()

    def __init__(self, *args, account, actor_role=None, actor_is_superuser=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = role_choices(
            account, actor_role, actor_is_superuser=actor_is_superuser
        )
        _style(self)


class UserEditForm(forms.ModelForm):
    """A member's login details: only for people managed by this account alone."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        _style(self)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if email and _email_taken(email, exclude_pk=self.instance.pk):
            raise forms.ValidationError("Another account already uses this email.")
        return email


class ProfileForm(forms.ModelForm):
    current_password = forms.CharField(
        label="Current password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Needed only when you change your email.",
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
        help_texts = {"email": "Used for password resets."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_email = self.instance.email
        _style(self)

    def clean(self):
        data = super().clean()
        # Changing where reset links go is as sensitive as changing the password: a stolen
        # session alone must not be enough to take the account over for good.
        if (data.get("email") or "").lower() != (self.original_email or "").lower():
            if not self.instance.check_password(data.get("current_password") or ""):
                self.add_error(
                    "current_password", "Enter your current password to change your email."
                )
        return data

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if email and _email_taken(email, exclude_pk=self.instance.pk):
            raise forms.ValidationError("Another account already uses this email.")
        return email


def styled(form):
    """Apply the dashboard's input classes to a stock Django form (password change/set)."""
    _style(form)
    return form


__all__ = [
    "AcceptInviteForm",
    "AccountKindForm",
    "ForgotPasswordForm",
    "Invitation",
    "InviteForm",
    "LoginForm",
    "MemberRoleForm",
    "ProfileForm",
    "SignupForm",
    "UserEditForm",
    "styled",
]
