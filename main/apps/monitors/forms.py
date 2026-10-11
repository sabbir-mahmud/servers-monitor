from django import forms

from .models import Monitor


class MonitorForm(forms.ModelForm):
    class Meta:
        model = Monitor
        fields = [
            "name",
            "url",
            "method",
            "interval_seconds",
            "timeout_seconds",
            "expected_status",
            "keyword",
            "failure_threshold",
            "verify_tls",
            "server",
            "is_enabled",
        ]
        labels = {
            "url": "URL",
            "interval_seconds": "Check every (seconds)",
            "timeout_seconds": "Timeout (seconds)",
            "expected_status": "Expected status",
            "verify_tls": "Verify TLS certificate",
            "is_enabled": "Enabled",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["server"].empty_label = "None"
        self.fields["url"].widget.attrs["placeholder"] = "https://app.example.com/health"
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def clean_url(self):
        url = self.cleaned_data["url"]
        if not url.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Only http:// and https:// URLs can be monitored.")
        return url
