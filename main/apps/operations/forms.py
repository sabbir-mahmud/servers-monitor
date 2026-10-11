from django import forms

from apps.fleet.models import Environment, Server, ServerGroup
from apps.monitors.models import Monitor

from .cron import CronError, validate
from .models import Schedule, Strategy

BUILTIN_LABELS = {
    "tail_log": "Read log (tail)",
    "tail_follow": "Follow log (live)",
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
            ("tail_follow", BUILTIN_LABELS["tail_follow"]),
            ("restart_service", BUILTIN_LABELS["restart_service"]),
        ]
        if user is not None and user.has_perm("operations.run_shell"):
            choices.append(("shell", BUILTIN_LABELS["shell"]))
        if custom:
            choices.append(("Agent actions", [(n, f"{n} — {d}") for n, d in custom]))
        self.fields["action"].choices = choices
        self.fields["health_gates"].queryset = Monitor.objects.filter(is_enabled=True).order_by(
            "name"
        )
        for name in ("batch_size", "pause_seconds"):
            self.fields[name].widget.attrs["class"] = "form-control"
        for name in ("rolling", "stop_on_failure"):
            self.fields[name].widget.attrs["class"] = "form-check-input"
        self.fields["rolling"].widget.attrs["data-rolling-toggle"] = ""
        for name in ("action", "name", "lines", "note", "filter"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["action"].widget.attrs["class"] = "form-select"
        self.fields["command"].widget.attrs["class"] = "form-control font-monospace"

    def action_args(self) -> dict:
        d = self.cleaned_data
        return {"name": d.get("name"), "lines": d.get("lines"), "command": d.get("command"),
                "filter": d.get("filter")}  # fmt: skip

    def rollout(self) -> dict | None:
        d = self.cleaned_data
        if not d.get("rolling"):
            return None
        return {"strategy": Strategy.ROLLING, "batch_size": d.get("batch_size") or 1,
                "pause_seconds": d.get("pause_seconds") or 0, "stop_on_failure": d.get("stop_on_failure", False),
                "health_gates": list(d.get("health_gates") or [])}  # fmt: skip


class ScheduleForm(JobForm):
    """A job form plus when to run it and how to pick servers at run time."""

    sched_name = forms.CharField(max_length=100, label="Name")
    cron = forms.CharField(max_length=100, label="When (cron)", initial="0 3 * * *",
                           help_text="minute hour day month weekday in the dashboard's time zone, "
                                     "e.g. 0 3 * * * (daily 03:00), 30 2 * * mon (Mondays), or @hourly.")  # fmt: skip
    groups = forms.ModelMultipleChoiceField(queryset=ServerGroup.objects.all(), required=False,
                                            widget=forms.CheckboxSelectMultiple, label="Plus every server in groups")  # fmt: skip
    environments = forms.MultipleChoiceField(choices=Environment.choices, required=False,
                                             widget=forms.CheckboxSelectMultiple, label="Plus every server in environments")  # fmt: skip
    is_enabled = forms.BooleanField(required=False, initial=True, label="Enabled")

    def __init__(self, *args, instance: Schedule | None = None, **kwargs):
        self.instance = instance
        if instance is not None:
            kwargs.setdefault("initial", {}).update(
                sched_name=instance.name, cron=instance.cron, action=instance.action, note="",
                name=instance.args.get("name"), lines=instance.args.get("lines"),
                command=instance.args.get("command"), servers=list(instance.servers.all()),
                groups=list(instance.groups.all()), environments=instance.environments,
                is_enabled=instance.is_enabled, rolling=instance.strategy == Strategy.ROLLING,
                batch_size=instance.batch_size, pause_seconds=instance.pause_seconds,
                stop_on_failure=instance.stop_on_failure, health_gates=list(instance.health_gates.all()),
            )  # fmt: skip
        super().__init__(*args, **kwargs)
        self.fields["servers"].required = False
        self.fields["sched_name"].widget.attrs["class"] = "form-control"
        self.fields["cron"].widget.attrs["class"] = "form-control mono"
        self.fields["is_enabled"].widget.attrs["class"] = "form-check-input"

    def clean_cron(self):
        try:
            return validate(self.cleaned_data["cron"])
        except CronError as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_sched_name(self):
        name = self.cleaned_data["sched_name"].strip()
        clash = Schedule.objects.filter(name=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another schedule has this name.")
        return name

    def clean(self):
        data = super().clean()
        from .services import JobError, clean_args

        if not (data.get("servers") or data.get("groups") or data.get("environments")):
            raise forms.ValidationError("Choose servers, groups or environments to run on.")
        if data.get("action"):
            try:
                self.clean_action_args = clean_args(data["action"], self.action_args())
            except JobError as exc:
                raise forms.ValidationError(str(exc)) from None
        return data

    def save(self, user) -> Schedule:
        from django.utils import timezone

        from .services import schedule_next

        d = self.cleaned_data
        sched = self.instance or Schedule(created_by=user)
        sched.name, sched.cron, sched.action = d["sched_name"], d["cron"], d["action"]
        sched.args = self.clean_action_args
        sched.environments = list(d.get("environments") or [])
        sched.is_enabled = bool(d.get("is_enabled"))
        sched.strategy = Strategy.ROLLING if d.get("rolling") else Strategy.ALL
        sched.batch_size = d.get("batch_size") or 1
        sched.pause_seconds = d.get("pause_seconds") or 0
        sched.stop_on_failure = bool(d.get("stop_on_failure"))
        sched.next_run_at = schedule_next(sched, timezone.now()) if sched.is_enabled else None
        sched.save()
        sched.servers.set(d.get("servers") or [])
        sched.groups.set(d.get("groups") or [])
        sched.health_gates.set(d.get("health_gates") or [])
        return sched
