from django import forms

from apps.fleet.models import Environment, Server
from apps.monitors.models import Monitor

from .channels import KINDS
from .models import Channel, ChannelKind, MaintenanceWindow

REPEAT_CHOICES = [
    (0, "Never"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (240, "Every 4 hours"),
]


def _style(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.CheckboxSelectMultiple):
            continue
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, forms.Select):
            w.attrs.setdefault("class", "form-select")
        else:
            w.attrs.setdefault("class", "form-control")


class ChannelForm(forms.ModelForm):
    environments = forms.MultipleChoiceField(
        choices=Environment.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unticked to include every environment.",
    )
    repeat_minutes = forms.TypedChoiceField(
        choices=REPEAT_CHOICES, coerce=int, label="Remind while unacknowledged"
    )

    class Meta:
        model = Channel
        fields = [
            "name",
            "telegram_token",
            "telegram_chat_id",
            "webhook_url",
            "min_severity",
            "environments",
            "include_servers",
            "include_apps",
            "notify_resolved",
            "repeat_minutes",
            "is_enabled",
        ]
        labels = {
            "telegram_token": "Bot token",
            "telegram_chat_id": "Chat ID",
            "webhook_url": "Webhook URL",
            "min_severity": "Minimum severity",
            "include_servers": "Server alerts (offline, CPU, memory, disk, services)",
            "include_apps": "Application alerts (down, certificate expiring)",
            "notify_resolved": "Also send a message when an alert resolves",
            "is_enabled": "Enabled",
        }
        widgets = {
            # Secrets are never sent back to the browser; blank on edit keeps the saved one.
            "telegram_token": forms.PasswordInput(render_value=False),
            "webhook_url": forms.PasswordInput(render_value=False),
        }
        # Encrypted text columns would default to a textarea and no length limit.
        field_classes = {
            "telegram_token": forms.CharField,
            "webhook_url": forms.CharField,
        }

    def __init__(self, *args, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = self.instance.kind or kind or ChannelKind.TELEGRAM
        # UUID primary keys exist before the first save, so ask Django instead of the pk.
        editing = not self.instance._state.adding
        if self.kind == ChannelKind.TELEGRAM:
            del self.fields["webhook_url"]
            self.fields["telegram_token"].help_text = (
                "Create a bot with @BotFather in Telegram and paste its token."
                + (" Leave blank to keep the saved token." if editing else "")
            )
            self.fields[
                "telegram_chat_id"
            ].help_text = (
                "Send your bot any message (or add it to a group), then use Find my chats."
            )
        else:
            del self.fields["telegram_token"]
            del self.fields["telegram_chat_id"]
            self.fields["webhook_url"].help_text = (
                "Slack, Discord, Mattermost or Teams incoming-webhook URL."
                + (" Leave blank to keep the saved URL." if editing else "")
            )
        for name in ("telegram_token", "webhook_url"):
            if name in self.fields:
                self.fields[name].required = not editing
        self.kind_spec = KINDS.get(self.kind)
        if self.kind_spec:
            for name in ("telegram_token", "telegram_chat_id", "webhook_url"):
                self.fields.pop(name, None)
            saved = self.instance.settings if editing else {}
            for setting in self.kind_spec.settings:
                keep = setting.secret and editing and saved.get(setting.name)
                field = forms.CharField(
                    label=setting.label,
                    # Hidden settings are filled by the page's script: one clear error below.
                    required=setting.required and not keep and not setting.hidden,
                    max_length=500,
                    help_text=setting.help
                    + (" Leave blank to keep the saved value." if keep else ""),
                    widget=forms.HiddenInput()
                    if setting.hidden
                    else forms.PasswordInput(render_value=False)
                    if setting.secret
                    else forms.TextInput(),
                    initial=None if setting.secret else saved.get(setting.name, ""),
                )
                if setting.placeholder:
                    field.widget.attrs["placeholder"] = setting.placeholder
                self.fields[f"cfg_{setting.name}"] = field
        self.fields["min_severity"].help_text = "Warning includes critical alerts too."
        _style(self)

    @property
    def config_fields(self):
        return [self[name] for name in self.fields if name.startswith("cfg_")]

    def clean(self):
        data = super().clean()
        for secret in ("telegram_token", "webhook_url"):
            if secret in self.fields and not data.get(secret) and not self.instance._state.adding:
                data[secret] = getattr(self.instance, secret)  # keep the saved secret
        if self.kind == ChannelKind.WEBHOOK and data.get("webhook_url"):
            from django.core.validators import URLValidator

            from apps.core.netguard import check_url_host

            try:
                URLValidator(schemes=["https"])(data["webhook_url"])
            except forms.ValidationError:
                self.add_error("webhook_url", "Enter the full https:// webhook URL.")
                return data
            if not data["webhook_url"].startswith("https://"):
                self.add_error("webhook_url", "Use the https:// URL your chat service gave you.")
            elif problem := check_url_host(data["webhook_url"]):
                self.add_error("webhook_url", problem)
        if self.kind_spec:
            self._clean_config(data)
        if not data.get("include_servers") and not data.get("include_apps"):
            raise forms.ValidationError("Choose server alerts, application alerts or both.")
        return data

    def _clean_config(self, data):
        import json

        from django.core.validators import URLValidator, validate_email

        from apps.core.netguard import check_url_host

        config = dict(self.instance.settings) if not self.instance._state.adding else {}
        for setting in self.kind_spec.settings:
            field = f"cfg_{setting.name}"
            value = (data.get(field) or "").strip()
            if not value and setting.secret and config.get(setting.name):
                continue  # keep the saved secret
            if not value and setting.hidden and setting.required:
                self.add_error(None, "Press Subscribe this browser first, and allow notifications.")
                return
            if value and setting.url:
                try:
                    URLValidator(schemes=["https", "http"])(value)
                except forms.ValidationError:
                    self.add_error(field, "Enter the full https:// URL.")
                    continue
                if problem := check_url_host(value):
                    self.add_error(field, problem)
                    continue
            if value and setting.name == "recipients":
                addresses = [a.strip() for a in value.split(",") if a.strip()]
                try:
                    for address in addresses:
                        validate_email(address)
                except forms.ValidationError:
                    self.add_error(field, f"{address!r} isn't an email address.")
                    continue
                value = ", ".join(addresses)
            if value:
                config[setting.name] = value
            else:
                config.pop(setting.name, None)
        self.config_json = json.dumps(config)

    def save(self, commit=True):
        if self.kind_spec:
            self.instance.config = self.config_json
        self.instance.kind = self.kind
        for secret in ("telegram_token", "webhook_url"):
            if secret in self.cleaned_data:
                setattr(self.instance, secret, self.cleaned_data[secret])
        return super().save(commit)


class MaintenanceForm(forms.ModelForm):
    starts_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Starts",
    )
    ends_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Ends",
    )
    servers = forms.ModelMultipleChoiceField(
        queryset=Server.objects.all(), required=False, widget=forms.CheckboxSelectMultiple
    )
    monitors = forms.ModelMultipleChoiceField(
        queryset=Monitor.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Applications",
    )

    class Meta:
        model = MaintenanceWindow
        fields = ["title", "starts_at", "ends_at", "all_targets", "servers", "monitors"]
        labels = {"title": "What's happening", "all_targets": "Everything (all servers and apps)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs["placeholder"] = "Database upgrade"
        self.fields["servers"].help_text = "Muting a server also mutes the applications on it."
        _style(self)

    def clean(self):
        data = super().clean()
        start, end = data.get("starts_at"), data.get("ends_at")
        if start and end and end <= start:
            self.add_error("ends_at", "The window must end after it starts.")
        if not (data.get("all_targets") or data.get("servers") or data.get("monitors")):
            raise forms.ValidationError("Choose at least one server or application, or Everything.")
        return data
