from django import forms

from apps.fleet.models import Server
from apps.monitors.models import Monitor
from apps.tenants.scoping import AccountFormMixin

from .models import Project


class ProjectForm(AccountFormMixin, forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repo_url", "branch", "app", "servers", "mode", "steps", "strategy",
                  "batch_size", "stop_on_failure", "health_gates", "is_enabled"]  # fmt: skip
        widgets = {
            "servers": forms.CheckboxSelectMultiple,
            "health_gates": forms.CheckboxSelectMultiple,
            "mode": forms.RadioSelect,
            "steps": forms.Textarea(attrs={
                "rows": 12, "spellcheck": "false", "wrap": "off",
                "placeholder": "npm ci\nnpm run build\nsudo systemctl restart api  # needs its own sudo rule",
            }),
        }  # fmt: skip
        labels = {"is_enabled": "Enabled", "stop_on_failure": "Stop if a server fails",
                  "batch_size": "Servers per batch", "health_gates": "Wait for these monitors"}  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["servers"].queryset = Server.objects.filter(account=self.account).order_by(
            "name"
        )
        self.fields["health_gates"].queryset = Monitor.objects.filter(
            account=self.account, is_enabled=True
        ).order_by("name")
        self.fields["strategy"].label = "Roll out"
        for name in ("name", "repo_url", "branch", "app", "batch_size"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        for name in ("repo_url", "branch", "app"):
            self.fields[name].widget.attrs["class"] += " mono"
            self.fields[name].widget.attrs["spellcheck"] = "false"
        self.fields["repo_url"].widget.attrs["placeholder"] = "git@github.com:acme/api.git"
        self.fields["app"].widget.attrs["placeholder"] = "api"
        self.fields["strategy"].widget.attrs["class"] = "form-select"
        self.fields["steps"].widget.attrs["class"] = "form-control mono script-editor"
        for name in ("stop_on_failure", "is_enabled"):
            self.fields[name].widget.attrs["class"] = "form-check-input"

    def clean_repo_url(self):
        url = self.cleaned_data["repo_url"].strip()
        if url.startswith("-") or any(c.isspace() for c in url):
            raise forms.ValidationError("That isn't a git URL.")
        return url

    def clean_steps(self):
        steps = self.cleaned_data["steps"].replace("\r\n", "\n").strip("\n")
        return steps + "\n" if steps.strip() else ""
