from django import forms

from apps.tenants.forms import _style
from apps.tenants.scoping import AccountFormMixin

from . import domains
from .models import WEEKDAYS, Device, DeviceControls, Place, Platform


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
        fields = ["screen_time", "web_activity", "location", "block_adult", "safe_search",
                  "blocked_domains", "allowed_domains"]  # fmt: skip
        labels = {
            "screen_time": "Screen time",
            "web_activity": "Websites visited",
            "location": "Location",
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
        for name in ("screen_time", "web_activity", "location", "block_adult", "safe_search"):
            self.fields[name].widget.attrs["role"] = "switch"
        _style(self)

    def _domains(self, field):
        return "\n".join(domains.parse_list(self.cleaned_data.get(field, "")))

    def clean_blocked_domains(self):
        return self._domains("blocked_domains")

    def clean_allowed_domains(self):
        return self._domains("allowed_domains")


COORDS = __import__("re").compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")


def parse_location(text: str) -> tuple[float, float] | None:
    """ "23.8103, 90.4125", or a map link with the coordinates in it (Google Maps "@lat,lon",
    OpenStreetMap "#map=z/lat/lon", "?q=lat,lon")."""
    text = (text or "").strip()
    if "#map=" in text:
        parts = text.split("#map=", 1)[1].split("/")
        if len(parts) >= 3:
            text = f"{parts[1]},{parts[2]}"
    elif "@" in text:
        text = text.split("@", 1)[1]
    match = COORDS.search(text)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


class PlaceForm(AccountFormMixin, forms.ModelForm):
    location = forms.CharField(
        required=False,
        label="Where",
        help_text="Coordinates (23.8103, 90.4125) or a link copied from Google Maps or "
        "OpenStreetMap.",
    )
    from_device = forms.ModelChoiceField(
        Device.objects.exclude(latitude=None), required=False,
        label="Or use where a device is now", empty_label="—",
    )  # fmt: skip
    arrive_days = forms.TypedMultipleChoiceField(
        choices=WEEKDAYS, coerce=int, required=False, widget=forms.CheckboxSelectMultiple,
        label="On",
    )  # fmt: skip

    class Meta:
        model = Place
        fields = ["name", "radius_m", "notify_arrive", "notify_leave", "people", "arrive_by",
                  "arrive_days"]  # fmt: skip
        labels = {
            "name": "Name",
            "notify_arrive": "Tell me when someone arrives",
            "notify_leave": "Tell me when someone leaves",
            "people": "Who",
            "arrive_by": "Expect them by",
        }
        help_texts = {
            "name": "Home, School, Grandma's.",
            "people": "Nobody ticked: everyone's devices.",
            "arrive_by": "Optional. If they haven't arrived by then on these days, you get an "
            "alert. Needs people ticked above.",
        }
        widgets = {
            "people": forms.CheckboxSelectMultiple,
            "arrive_by": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope_choices()
        self.fields["radius_m"].label = "Radius (metres)"  # around the point
        self.fields["radius_m"].help_text = "100–300 m suits a house or a school."
        if self.instance.pk:
            self.fields["location"].initial = f"{self.instance.latitude}, {self.instance.longitude}"
        else:
            self.fields["arrive_days"].initial = [0, 1, 2, 3, 4]
        _style(self)

    def clean_radius_m(self):
        radius = self.cleaned_data["radius_m"]
        if not 25 <= radius <= 5000:
            raise forms.ValidationError("Between 25 and 5000 metres.")
        return radius

    def clean(self):
        data = super().clean()
        device = data.get("from_device")
        if device is not None:
            self.instance.latitude, self.instance.longitude = device.latitude, device.longitude
        elif coords := parse_location(data.get("location", "")):
            self.instance.latitude, self.instance.longitude = (round(coords[0], 6),
                                                               round(coords[1], 6))  # fmt: skip
        elif data.get("location") or self.instance.latitude is None:
            self.add_error("location", "Type coordinates or paste a map link, or pick a device.")
        if data.get("arrive_by") and not data.get("people"):
            self.add_error("people", "Tick who's expected there.")
        if data.get("arrive_by") and not data.get("arrive_days"):
            self.add_error("arrive_days", "Pick at least one day.")
        return data
