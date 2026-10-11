from django import forms

from .models import Server, ServerGroup


class ServerForm(forms.ModelForm):
    groups = forms.ModelMultipleChoiceField(
        queryset=ServerGroup.objects.all(), required=False, widget=forms.CheckboxSelectMultiple
    )
    new_groups = forms.CharField(
        required=False,
        label="New groups",
        help_text="Comma-separated, e.g. api, workers. Created if they don't exist.",
    )

    class Meta:
        model = Server
        fields = [
            "name",
            "environment",
            "groups",
            "description",
            "report_interval",
            "is_enabled",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif not isinstance(field.widget, forms.CheckboxSelectMultiple):
                field.widget.attrs.setdefault("class", "form-control")

    def clean_new_groups(self):
        from django.utils.text import slugify

        raw = self.cleaned_data.get("new_groups", "")
        return [s for s in (slugify(p) for p in raw.split(",")) if s]

    def _save_m2m(self):
        super()._save_m2m()
        for slug in self.cleaned_data.get("new_groups", []):
            group, _ = ServerGroup.objects.get_or_create(name=slug)
            self.instance.groups.add(group)
