from django import forms

from apps.fleet.models import Server

BUILTIN_LABELS = {
    "tail_log": "Read log (tail)",
    "restart_service": "Restart service",
    "shell": "Shell command (admin)",
}


class JobForm(forms.Form):
    action = forms.ChoiceField()
    name = forms.CharField(
        required=False,
        max_length=100,
        label="Log / service name",
        help_text="For 'Read log' and 'Restart service'.",
    )
    lines = forms.IntegerField(required=False, min_value=1, max_value=2000, initial=200)
    command = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "font-monospace"}),
        help_text="Runs via /bin/sh -c on servers whose agent has shell enabled.",
    )
    note = forms.CharField(required=False, max_length=200)
    servers = forms.ModelMultipleChoiceField(
        queryset=Server.objects.none(), widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        servers = Server.objects.filter(is_enabled=True).prefetch_related("groups")
        self.fields["servers"].queryset = servers
        custom = sorted(
            {
                (a["name"], a.get("description") or a["name"])
                for s in servers.only("capabilities")
                for a in s.actions
                if a.get("name") not in BUILTIN_LABELS
            }
        )
        choices = [
            ("tail_log", BUILTIN_LABELS["tail_log"]),
            ("restart_service", BUILTIN_LABELS["restart_service"]),
        ]
        if user is not None and user.has_perm("operations.run_shell"):
            choices.append(("shell", BUILTIN_LABELS["shell"]))
        if custom:
            choices.append(("Agent actions", [(n, f"{n} — {d}") for n, d in custom]))
        self.fields["action"].choices = choices
        for name in ("action", "name", "lines", "note"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["action"].widget.attrs["class"] = "form-select"
        self.fields["command"].widget.attrs["class"] = "form-control font-monospace"

    def action_args(self) -> dict:
        d = self.cleaned_data
        return {"name": d.get("name"), "lines": d.get("lines"), "command": d.get("command")}
