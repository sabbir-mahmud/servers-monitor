from django import forms

from .models import FAMILY_KINDS, Account, Person, PersonKind
from .roles import Kind
from .scoping import AccountFormMixin


def _style(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.RadioSelect | forms.CheckboxSelectMultiple):
            continue
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, forms.Select):
            w.attrs.setdefault("class", "form-select")
        else:
            w.attrs.setdefault("class", "form-control")


class AccountSettingsForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["name"]
        labels = {"name": "Account name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class PairingSettingsForm(forms.ModelForm):
    """How computers pair: the NexGuard app only (the default), or the one-line command too."""

    class Meta:
        model = Account
        fields = ["computer_pairing"]
        labels = {"computer_pairing": "How computers connect"}
        widgets = {"computer_pairing": forms.RadioSelect}


class PersonForm(AccountFormMixin, forms.ModelForm):
    class Meta:
        model = Person
        fields = ["kind", "name", "email", "notes"]
        labels = {"notes": "Note", "kind": "Who is this?"}
        widgets = {"kind": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        family = self.account is not None and self.account.kind == Kind.FAMILY
        if family:
            # A family picks the relationship; the choices never include "staff".
            self.fields["kind"].choices = [(k.value, k.label) for k in FAMILY_KINDS]
            if self.instance._state.adding:
                self.initial.setdefault("kind", PersonKind.CHILD)
        else:
            del self.fields["kind"]
            if self.instance._state.adding:
                self.instance.kind = PersonKind.STAFF
        if family:
            self.fields["name"].widget.attrs["placeholder"] = "Sara"
            self.fields["email"].help_text = "Optional."
            self.fields["notes"].widget.attrs["placeholder"] = (
                "e.g. Year 7, has an iPad and a laptop"
            )
        else:
            self.fields["name"].widget.attrs["placeholder"] = "Jordan Lee"
            self.fields["notes"].widget.attrs["placeholder"] = "e.g. Support team, London office"
        _style(self)
