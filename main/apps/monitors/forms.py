import ipaddress
import json

from django import forms

from .models import DnsType, Kind, Method, Monitor

ALL = {k.value for k in Kind}
PROBED = ALL - {Kind.HEARTBEAT}

# Which fields each kind uses. The template tags every field with its kinds and a few
# lines of JS show only the relevant ones; clean() ignores the rest.
FIELD_KINDS = {
    "name": ALL,
    "kind": ALL,
    "url": {Kind.HTTP},
    "method": {Kind.HTTP},
    "target": ALL - {Kind.HTTP},
    "port": {Kind.TCP},
    "tls": {Kind.TCP},
    "record_type": {Kind.DNS},
    "expected": {Kind.DNS},
    "resolver": {Kind.DNS},
    "period_seconds": {Kind.HEARTBEAT},
    "grace_seconds": {Kind.HEARTBEAT},
    "interval_seconds": PROBED - {Kind.DOMAIN},
    "timeout_seconds": PROBED,
    "failure_threshold": PROBED - {Kind.DOMAIN},
    "expected_status": {Kind.HTTP},
    "keyword": {Kind.HTTP},
    "keyword_absent": {Kind.HTTP},
    "follow_redirects": {Kind.HTTP},
    "body": {Kind.HTTP},
    "headers": {Kind.HTTP},
    "clear_headers": {Kind.HTTP},
    "slow_ms": {Kind.HTTP, Kind.TCP, Kind.PING, Kind.DNS},
    "verify_tls": {Kind.HTTP, Kind.TCP},
    "agents_only": {Kind.HTTP, Kind.TCP},
    "probe_servers": {Kind.HTTP, Kind.TCP},
    "quorum": {Kind.HTTP, Kind.TCP},
    "server": ALL,
    "is_enabled": ALL,
}

# The target field's label and placeholder change with the kind.
TARGET_LABELS = {
    Kind.TCP: ("Host", "db.internal.example.com"),
    Kind.PING: ("Host", "203.0.113.10 or router.example.com"),
    Kind.DNS: ("Name to look up", "example.com"),
    Kind.DOMAIN: ("Domain", "example.com"),
    Kind.HEARTBEAT: ("Job name", "nightly-backup"),
}

# Option fields stored in Monitor.options, per kind.
OPTION_FIELDS = {
    Kind.HTTP: ["keyword_absent", "follow_redirects", "body", "slow_ms"],
    Kind.TCP: ["port", "tls", "slow_ms"],
    Kind.PING: ["slow_ms"],
    Kind.DNS: ["record_type", "expected", "resolver", "slow_ms"],
    Kind.DOMAIN: [],
    Kind.HEARTBEAT: ["period_seconds", "grace_seconds"],
}


def _parse_headers(text: str) -> dict:
    headers = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        name = name.strip()
        if not sep or not name or any(c in name for c in ' \t()<>@,;\\"/[]?={}'):
            raise forms.ValidationError(f"Line {n}: use the form Name: value.")
        if "\r" in value or "\n" in value:
            raise forms.ValidationError(f"Line {n}: header values can't contain line breaks.")
        headers[name] = value.strip()
    if len(headers) > 20:
        raise forms.ValidationError("At most 20 headers.")
    return headers


class MonitorForm(forms.ModelForm):
    port = forms.IntegerField(required=False, min_value=1, max_value=65535, label="Port")
    tls = forms.BooleanField(required=False, label="Complete a TLS handshake",
                             help_text="Also checks the certificate and warns before it expires.")  # fmt: skip
    record_type = forms.ChoiceField(choices=DnsType.choices, initial=DnsType.A, required=False,
                                    label="Record type")  # fmt: skip
    expected = forms.CharField(
        required=False, max_length=200, label="Expected answer",
        help_text="Optional: an answer must contain this, e.g. an IP or mail host. Empty = any answer.",
    )  # fmt: skip
    resolver = forms.CharField(
        required=False, max_length=45, label="DNS server",
        help_text="Optional resolver IP, e.g. 1.1.1.1. Empty = this machine's resolver.",
    )  # fmt: skip
    period_seconds = forms.IntegerField(
        required=False, min_value=60, max_value=31 * 86400, initial=86400, label="Expected every (seconds)",
        help_text="How often the job runs: 3600 = hourly, 86400 = daily, 604800 = weekly.",
    )  # fmt: skip
    grace_seconds = forms.IntegerField(
        required=False, min_value=60, max_value=7 * 86400, initial=3600, label="Grace (seconds)",
        help_text="Extra time allowed before a missing ping counts as down.",
    )  # fmt: skip
    keyword_absent = forms.BooleanField(
        required=False, label="Down if the keyword appears (instead of when it's missing)",
        help_text='Useful for "Maintenance", "Error" or "Exception" on a page that still answers 200.',
    )  # fmt: skip
    follow_redirects = forms.BooleanField(required=False, initial=True, label="Follow redirects")
    body = forms.CharField(required=False, max_length=4000, label="Request body",
                           widget=forms.Textarea(attrs={"rows": 3}), help_text="Sent with POST and PUT.")  # fmt: skip
    headers = forms.CharField(
        required=False, max_length=4000, label="Request headers",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Authorization: Bearer …"}),
        help_text="One per line, Name: value. Stored encrypted.",
    )  # fmt: skip
    clear_headers = forms.BooleanField(required=False, label="Remove the saved headers")
    slow_ms = forms.IntegerField(
        required=False, min_value=1, max_value=60_000, label="Slow above (ms)",
        help_text="Optional: answers slower than this show as Slow (amber), not down.",
    )  # fmt: skip

    agents_only = forms.BooleanField(
        required=False, label="Only from the servers below, not from this dashboard",
        help_text="For internal URLs the dashboard can't reach.",
    )  # fmt: skip

    class Meta:
        model = Monitor
        fields = [
            "kind",
            "name",
            "url",
            "method",
            "target",
            "interval_seconds",
            "timeout_seconds",
            "expected_status",
            "keyword",
            "failure_threshold",
            "verify_tls",
            "probe_servers",
            "quorum",
            "server",
            "is_enabled",
        ]
        widgets = {"probe_servers": forms.CheckboxSelectMultiple}
        labels = {
            "probe_servers": "Also check from these servers",
            "quorum": "Down when this many locations fail",
            "kind": "Type",
            "url": "URL",
            "target": "Host",
            "interval_seconds": "Check every (seconds)",
            "timeout_seconds": "Timeout (seconds)",
            "expected_status": "Expected status",
            "verify_tls": "Verify TLS certificate",
            "is_enabled": "Enabled",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        self.saved_headers = instance.headers if instance.pk else {}
        for key in {f for names in OPTION_FIELDS.values() for f in names}:
            if key in instance.options:
                self.initial[key] = instance.options[key]
        if self.saved_headers:
            self.fields[
                "headers"
            ].help_text = f"{len(self.saved_headers)} header(s) saved and hidden. Enter new ones to replace them."
        else:
            del self.fields["clear_headers"]
        self.fields["server"].empty_label = "None"
        self.initial["agents_only"] = bool(instance.pk) and not instance.check_from_dashboard
        from apps.fleet.models import Server

        servers = list(Server.objects.filter(is_enabled=True).only("name", "capabilities"))
        self.fields["probe_servers"].queryset = Server.objects.filter(is_enabled=True)
        self.fields["probe_servers"].required = False
        self.fields["quorum"].required = False  # older clients post none: 1
        self.probe_ready = {
            str(s.pk) for s in servers if "probes" in (s.capabilities.get("features") or [])
        }
        self.fields["probe_servers"].label_from_instance = lambda s: (
            s.name
            if str(s.pk) in self.probe_ready
            else f"{s.name} (probes off: sudo smctl probes on)"
        )
        self.fields["url"].widget.attrs["placeholder"] = "https://app.example.com/health"
        self.fields["target"].help_text = ""
        for name, field in self.fields.items():
            kinds = FIELD_KINDS.get(name, ALL)
            field.widget.attrs["data-kinds"] = " ".join(sorted(kinds))
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")
        self.fields["kind"].widget.attrs["data-kind-select"] = ""
        self.fields["kind"].required = False  # older clients post no kind: HTTP
        target = self.fields["target"].widget.attrs
        for kind, (label, placeholder) in TARGET_LABELS.items():
            target[f"data-label-{kind}"] = label
            target[f"data-placeholder-{kind}"] = placeholder

    def clean_url(self):
        url = self.cleaned_data["url"]
        if not url:
            return url
        if not url.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Only http:// and https:// URLs can be monitored.")
        from apps.core.netguard import check_url_host

        if self.data.get("agents_only"):
            return url  # only agents check it: internal URLs are the point (they guard metadata)
        if problem := check_url_host(url):
            raise forms.ValidationError(problem)
        return url

    def clean_kind(self):
        return self.cleaned_data.get("kind") or Kind.HTTP

    def clean_target(self):
        return (self.cleaned_data.get("target") or "").strip().lower().rstrip(".") or ""

    def clean_resolver(self):
        value = (self.cleaned_data.get("resolver") or "").strip()
        if not value:
            return ""
        try:
            ip = str(ipaddress.ip_address(value))
        except ValueError:
            raise forms.ValidationError("Enter the resolver's IP address.") from None
        from apps.core.netguard import describe_block, is_allowed

        if not is_allowed(ip):
            raise forms.ValidationError(describe_block(ip))
        return ip

    def clean_headers(self):
        return _parse_headers(self.cleaned_data.get("headers") or "")

    def clean(self):
        data = super().clean()
        kind = data.get("kind") or Kind.HTTP
        if kind == Kind.TCP and not data.get("port"):
            self.add_error("port", "Enter the port to connect to.")
        if kind == Kind.HEARTBEAT:
            data["failure_threshold"] = 1  # a missed or failed job is down at once
            data.setdefault("period_seconds", None)
            if not data.get("period_seconds"):
                data["period_seconds"] = 86400
            if not data.get("grace_seconds"):
                data["grace_seconds"] = 3600
        if kind == Kind.DOMAIN:
            data["interval_seconds"], data["failure_threshold"] = 3600, 1
        if kind != Kind.HTTP:
            data["method"] = Method.GET
            data["keyword"] = ""

        options = {}
        for key in OPTION_FIELDS[kind]:
            value = data.get(key)
            if value not in (None, "", False):
                options[key] = value
        if kind == Kind.HTTP:
            options["follow_redirects"] = bool(data.get("follow_redirects"))
        self.instance.options = options

        if kind in (Kind.HTTP, Kind.TCP):
            probes = list(data.get("probe_servers") or [])
            from_dashboard = not data.get("agents_only")
            locations = len(probes) + from_dashboard
            if not locations:
                self.add_error("probe_servers", "Choose the servers to check from.")
            self.instance.check_from_dashboard = from_dashboard
            data["quorum"] = self.instance.quorum = data.get("quorum") or 1
            if data["quorum"] > max(locations, 1):
                self.add_error("quorum", f"There are only {locations} location(s) to agree.")
            has_headers = (data.get("headers") or self.saved_headers) and not data.get(
                "clear_headers"
            )
            if probes and kind == Kind.HTTP and has_headers:
                self.add_error("probe_servers", "Monitors with secret request headers are only "
                               "checked from the dashboard: the relay would see the headers.")  # fmt: skip
        else:
            self.instance.check_from_dashboard = True
            data["probe_servers"], data["quorum"] = [], 1
            self.instance.quorum = 1

        headers = data.get("headers") or {}
        if kind != Kind.HTTP or data.get("clear_headers"):
            self.instance.secret_options = ""
        elif headers:
            self.instance.secret_options = json.dumps({"headers": headers})
        # else: keep the saved (hidden) headers
        for key in ("failure_threshold", "interval_seconds", "method", "keyword"):
            setattr(self.instance, key, data.get(key, getattr(self.instance, key)))
        return data
