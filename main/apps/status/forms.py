import json

from django import forms

from apps.monitors.models import Monitor

from .models import (
    IncidentStatus,
    StatusIncident,
    StatusPage,
    StatusPageMonitor,
    domain_validator,
    repo_validator,
)


def _style(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.CheckboxSelectMultiple):
            continue
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class StatusPageForm(forms.ModelForm):
    repo = forms.CharField(required=False, max_length=140, validators=[repo_validator], label="Repository",
                           help_text="owner/repository, e.g. acme/status. Enable Pages on it (or connect it to Cloudflare Pages).")  # fmt: skip
    branch = forms.CharField(required=False, max_length=100, initial="main", label="Branch")
    path = forms.CharField(required=False, max_length=100, label="Folder",
                           help_text="Optional subfolder, e.g. docs. Empty = the repository root.")  # fmt: skip
    domain = forms.CharField(required=False, max_length=253, validators=[domain_validator], label="Custom domain",
                             help_text="Optional, e.g. status.example.com (writes a CNAME file).")  # fmt: skip
    token = forms.CharField(required=False, max_length=300, label="Access token",
                            widget=forms.PasswordInput(render_value=False),
                            help_text="A fine-grained GitHub token for this one repository with Contents: read and write.")  # fmt: skip

    class Meta:
        model = StatusPage
        fields = [
            "name",
            "title",
            "description",
            "logo_url",
            "homepage_url",
            "show_uptime",
            "publish_kind",
        ]
        labels = {"logo_url": "Logo URL", "homepage_url": "Your website", "show_uptime": "Show uptime history",
                  "publish_kind": "Publish to"}  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cfg = self.instance.config if self.instance.pk else {}
        for key in ("repo", "branch", "path", "domain"):
            if cfg.get(key):
                self.initial[key] = cfg[key]
        if cfg.get("token"):
            self.fields["token"].help_text = "Saved and hidden. Leave blank to keep it."
        entries = {e.monitor_id: e for e in self.instance.entries.all()} if self.instance.pk else {}
        self.monitor_rows = []
        for m in Monitor.objects.order_by("name"):
            entry = entries.get(m.pk)
            pick = forms.BooleanField(required=False, initial=entry is not None, label=m.name)
            label = forms.CharField(required=False, max_length=80, initial=entry.display_name if entry else m.name,
                                    label=f"Public name for {m.name}")  # fmt: skip
            self.fields[f"m_{m.pk.hex}"] = pick
            self.fields[f"label_{m.pk.hex}"] = label
            self.monitor_rows.append((m, f"m_{m.pk.hex}", f"label_{m.pk.hex}"))
        _style(self)

    @property
    def monitor_fields(self):
        return [(m, self[pick], self[label]) for m, pick, label in self.monitor_rows]

    def clean_logo_url(self):
        url = self.cleaned_data["logo_url"]
        if url and not url.startswith("https://"):
            raise forms.ValidationError(
                "Use an https:// image URL (it's loaded by visitors' browsers)."
            )
        return url

    def clean(self):
        data = super().clean()
        cfg = dict(self.instance.config) if self.instance.pk else {}
        for key in ("repo", "branch", "path", "domain"):
            cfg[key] = (
                (data.get(key) or "").strip().strip("/")
                if key == "path"
                else (data.get(key) or "").strip()
            )
        if data.get("token"):
            cfg["token"] = data["token"].strip()
        if data.get("publish_kind") == "github":
            if not cfg.get("repo"):
                self.add_error("repo", "Enter the repository to publish to.")
            if not cfg.get("token"):
                self.add_error("token", "A token is needed to publish.")
        self.config_json = json.dumps({k: v for k, v in cfg.items() if v})
        self.picked = [
            (m, (data.get(label) or "").strip() or m.name)
            for m, pick, label in self.monitor_rows
            if data.get(pick)
        ]
        return data

    def save(self, commit=True):
        self.instance.publish_config = self.config_json
        self.instance.needs_publish = True
        page = super().save(commit)
        keep = []
        for position, (monitor, name) in enumerate(self.picked):
            entry, _ = StatusPageMonitor.objects.update_or_create(
                page=page, monitor=monitor, defaults={"display_name": name, "position": position}
            )
            keep.append(entry.pk)
        page.entries.exclude(pk__in=keep).delete()
        return page


class IncidentForm(forms.ModelForm):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=2000,
                              help_text="Shown publicly. Say what's affected and what you're doing.")  # fmt: skip

    class Meta:
        model = StatusIncident
        fields = ["title", "impact", "status", "affected"]
        widgets = {"affected": forms.CheckboxSelectMultiple}
        labels = {"affected": "Affected components"}

    def __init__(self, *args, page, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["affected"].queryset = page.entries.all()
        self.fields["affected"].required = False
        self.fields["status"].choices = [
            c for c in IncidentStatus.choices if c[0] != IncidentStatus.RESOLVED
        ]
        _style(self)


class IncidentUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=IncidentStatus.choices)
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=2000)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)
