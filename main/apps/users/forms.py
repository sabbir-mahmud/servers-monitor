from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.validators import UnicodeUsernameValidator

from .accounts import ROLE_HELP, ROLES
from .models import Invitation

User = get_user_model()
NO_ACCESS = ""
ROLE_CHOICES = [(r, r) for r in ROLES] + [(NO_ACCESS, "No access")]


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


class SignupForm(_NewAccountForm):
    email = forms.EmailField()
    field_order = ["email", "username", "first_name", "last_name", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(self.field_order)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if _email_taken(email):
            # Sign-up is approval-gated, so naming the clash here costs little and saves
            # the person a confused wait. Password reset never does this.
            raise forms.ValidationError("An account with this email already exists.")
        return email


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class InviteForm(forms.Form):
    email = forms.EmailField()
    role = forms.ChoiceField(
        choices=[(r, r) for r in ROLES], initial="Viewer", widget=forms.RadioSelect
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["placeholder"] = "teammate@company.com"
        self.role_help = ROLE_HELP
        _style(self)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if _email_taken(email):
            raise forms.ValidationError("Someone already has an account with this email.")
        return email


class UserEditForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, required=False)

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
    "ForgotPasswordForm",
    "Invitation",
    "InviteForm",
    "LoginForm",
    "ProfileForm",
    "SignupForm",
    "UserEditForm",
    "styled",
]
