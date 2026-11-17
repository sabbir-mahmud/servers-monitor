from django import forms

from .models import Account, Person, PersonKind
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


class PersonForm(AccountFormMixin, forms.ModelForm):
    class Meta:
        model = Person
        fields = ["name", "email", "notes"]
        labels = {"notes": "Note"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance._state.adding and self.account is not None:
            self.instance.kind = (
                PersonKind.CHILD if self.account.kind == Kind.FAMILY else PersonKind.STAFF
            )
        if self.account is not None and self.account.kind == Kind.FAMILY:
            self.fields["name"].widget.attrs["placeholder"] = "Sara"
            self.fields["email"].help_text = "Optional."
            self.fields["notes"].widget.attrs["placeholder"] = (
                "e.g. Year 7, has an iPad and a laptop"
            )
        else:
            self.fields["name"].widget.attrs["placeholder"] = "Jordan Lee"
            self.fields["notes"].widget.attrs["placeholder"] = "e.g. Support team, London office"
        _style(self)
