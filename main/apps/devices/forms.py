from django import forms

from apps.tenants.forms import _style
from apps.tenants.scoping import AccountFormMixin

from . import domains
from .models import Device, DeviceControls, Platform


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


class DeviceControlsForm(forms.ModelForm):
    class Meta:
        model = DeviceControls
        fields = ["screen_time", "web_activity", "block_adult", "safe_search",
                  "blocked_domains", "allowed_domains"]  # fmt: skip
        labels = {
            "screen_time": "Screen time",
            "web_activity": "Websites visited",
            "block_adult": "Block adult websites",
            "safe_search": "SafeSearch",
            "blocked_domains": "Also block",
            "allowed_domains": "Always allow",
        }
        widgets = {
            "blocked_domains": forms.Textarea(attrs={"rows": 4, "placeholder": "tiktok.com"}),
            "allowed_domains": forms.Textarea(attrs={"rows": 3, "placeholder": "school.edu"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("screen_time", "web_activity", "block_adult", "safe_search"):
            self.fields[name].widget.attrs["role"] = "switch"
        _style(self)

    def _domains(self, field):
        return "\n".join(domains.parse_list(self.cleaned_data.get(field, "")))

    def clean_blocked_domains(self):
        return self._domains("blocked_domains")

    def clean_allowed_domains(self):
        return self._domains("allowed_domains")
