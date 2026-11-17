from django import forms

from apps.tenants.forms import _style
from apps.tenants.scoping import AccountFormMixin

from .models import Device, Platform


class DeviceForm(AccountFormMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ["platform", "name", "person"]
        widgets = {"platform": forms.RadioSelect}
        labels = {"platform": "What kind of device?", "name": "Device name"}
        help_texts = {"name": 'As you\'d say it: "Sara\'s iPhone", "Work laptop".'}

    # A family picks from avatars; past this many people a dropdown is easier to use.
    MAX_PERSON_CHIPS = 12

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No "---------" card: a device is always one of the platforms.
        self.fields["platform"].choices = list(Platform.choices)
        person = self.fields["person"]
        if self.account is not None and self.account.kind == "solo":
            del self.fields["person"]  # just you: nobody to assign it to
            self.fields["name"].help_text = 'As you\'d say it: "Work laptop", "My Pixel".'
        elif self.account is not None:
            person.label = "Whose is it?" if self.account.kind == "family" else "Assigned to"
            person.empty_label = "Shared"
            if person.queryset.count() <= self.MAX_PERSON_CHIPS:
                person.widget = forms.RadioSelect(choices=person.choices)
        if not self.instance._state.adding and self.instance.is_paired:
            # The app on the device is for one platform; a different one needs a new entry.
            self.fields["platform"].disabled = True
        _style(self)
