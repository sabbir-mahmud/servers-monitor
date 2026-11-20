from django import forms

from apps.fleet.models import Server
from apps.monitors.models import Monitor
from apps.tenants.scoping import AccountFormMixin

from . import server_tasks
from .models import Strategy

TASK_PREFIX = server_tasks.ACTION + ":"  # "_task:upgrade" in the action menu

BUILTIN_LABELS = {
    "tail_log": "Read log (tail)",
    "tail_follow": "Follow log (live)",
    "restart_service": "Restart service",
    "shell": "Shell command (admin)",
}


def action_choices(servers, user) -> list:
    """The action menu: built-ins, server tasks (one entry each, "_task:upgrade") and the
    custom actions the given servers' agents offer."""
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
        ("tail_follow", BUILTIN_LABELS["tail_follow"]),
        ("restart_service", BUILTIN_LABELS["restart_service"]),
    ]
    if user is not None and user.has_perm("operations.run_shell"):
        choices.append(("shell", BUILTIN_LABELS["shell"]))
    tasks = [(TASK_PREFIX + t.key, t.label) for t in server_tasks.TASKS.values()
             if user is None or server_tasks.can_run(user, t.key)]  # fmt: skip
    if tasks:
        choices.append(("Server tasks", tasks))
    if custom:
        choices.append(("Agent actions", [(n, f"{n} — {d}") for n, d in custom]))
    return choices


class JobForm(AccountFormMixin, forms.Form):
    action = forms.ChoiceField()
    name = forms.CharField(
        required=False,
        max_length=100,
        label="Log / service name",
        help_text="For 'Read log' and 'Restart service'.",
    )
    lines = forms.IntegerField(required=False, min_value=1, max_value=2000, initial=200)
    filter = forms.CharField(required=False, max_length=200, label="Only lines containing")
    command = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "font-monospace"}),
        help_text="Runs via /bin/sh -c on servers whose agent has shell enabled.",
    )
    note = forms.CharField(required=False, max_length=200)
    servers = forms.ModelMultipleChoiceField(
        queryset=Server.objects.none(), widget=forms.CheckboxSelectMultiple
    )
    rolling = forms.BooleanField(required=False, label="Roll out in batches",
                                 help_text="Start with the first batch; continue only while things stay healthy.")  # fmt: skip
    batch_size = forms.IntegerField(
        required=False, min_value=1, max_value=500, initial=1, label="Servers per batch"
    )
    pause_seconds = forms.IntegerField(required=False, min_value=0, max_value=3600, initial=30,
                                       label="Pause between batches (seconds)")  # fmt: skip
    stop_on_failure = forms.BooleanField(
        required=False, initial=True, label="Stop if a server fails"
    )
    health_gates = forms.ModelMultipleChoiceField(
        queryset=Monitor.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Wait for these monitors to be up after each batch",
        help_text="Halts if they aren't up again within 10 minutes.",
    )  # fmt: skip

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        servers = Server.objects.filter(account=self.account, is_enabled=True).prefetch_related(
            "groups"
        )
        self.fields["servers"].queryset = servers
        choices = action_choices(servers, user)
        self.fields["action"].choices = choices
        self.fields["health_gates"].queryset = Monitor.objects.filter(
            account=self.account, is_enabled=True
        ).order_by("name")
        self.scope_choices()
        for name in ("batch_size", "pause_seconds"):
            self.fields[name].widget.attrs["class"] = "form-control"
        for name in ("rolling", "stop_on_failure"):
            self.fields[name].widget.attrs["class"] = "form-check-input"
        self.fields["rolling"].widget.attrs["data-rolling-toggle"] = ""
        for name in ("action", "name", "lines", "note", "filter"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["action"].widget.attrs["class"] = "form-select"
        self.fields["command"].widget.attrs["class"] = "form-control font-monospace"

    def clean(self):
        data = super().clean()
        action = data.get("action") or ""
        if action.startswith(TASK_PREFIX):  # one menu entry per task, one action underneath
            data["action"], data["task"] = server_tasks.ACTION, action.removeprefix(TASK_PREFIX)
        return data

    def action_args(self) -> dict:
        d = self.cleaned_data
        return {"name": d.get("name"), "lines": d.get("lines"), "command": d.get("command"),
                "filter": d.get("filter"), "task": d.get("task")}  # fmt: skip

    def rollout(self) -> dict | None:
        d = self.cleaned_data
        if not d.get("rolling"):
            return None
        return {"strategy": Strategy.ROLLING, "batch_size": d.get("batch_size") or 1,
                "pause_seconds": d.get("pause_seconds") or 0, "stop_on_failure": d.get("stop_on_failure", False),
                "health_gates": list(d.get("health_gates") or [])}  # fmt: skip
