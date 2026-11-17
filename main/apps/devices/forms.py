from django import forms

from apps.tenants.forms import _style
from apps.tenants.scoping import AccountFormMixin

from .models import Device


class DeviceForm(AccountFormMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ["platform", "name", "person"]
        widgets = {"platform": forms.RadioSelect}
        labels = {"platform": "What kind of device?", "name": "Device name"}
        help_texts = {"name": 'As you\'d say it: "Sara\'s iPhone", "Family laptop".'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        person = self.fields["person"]
        if self.account is not None:
            person.label = "Whose is it?" if self.account.kind == "family" else "Assigned to"
            person.empty_label = "Nobody in particular (shared)"
        if not self.instance._state.adding and self.instance.is_paired:
            # The app on the device is for one platform; a different one needs a new entry.
            self.fields["platform"].disabled = True
        _style(self)
