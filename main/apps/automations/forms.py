from django import forms

from apps.fleet.models import Environment, Server, ServerGroup
from apps.monitors.models import Monitor
from apps.operations import server_tasks
from apps.operations.cron import CronError, validate
from apps.operations.forms import TASK_PREFIX, action_choices
from apps.operations.models import Strategy
from apps.tenants.scoping import AccountFormMixin

from .models import Automation, Kind

MAX_SEND = 20000  # apps/operations/services.MAX_SCRIPT_LENGTH


def _norm(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip("\n")
    return text + "\n" if text.strip() else ""


class AutomationForm(AccountFormMixin, forms.ModelForm):
    """What runs, where, when, and how it rolls out: one form for every kind."""

    # Action arguments (kind=action). Prefixed: `name` is the automation's own.
    arg_name = forms.CharField(required=False, max_length=100, label="Log / service name")
    arg_lines = forms.IntegerField(required=False, min_value=1, max_value=2000, initial=200,
                                   label="Lines")  # fmt: skip
    arg_filter = forms.CharField(required=False, max_length=200, label="Only lines containing")
    arg_command = forms.CharField(required=False, label="Command", widget=forms.Textarea(
        attrs={"rows": 3, "spellcheck": "false"}))  # fmt: skip
    rolling = forms.BooleanField(required=False, label="Roll out in batches",
                                 help_text="Start with the first batch; continue only while things stay healthy.")  # fmt: skip

    class Meta:
        model = Automation
        fields = ["name", "description", "kind", "action", "interpreter", "body", "repo_url",
                  "branch", "app", "steps", "servers", "groups", "environments", "cron",
                  "on_push", "require_approval", "batch_size", "pause_seconds",
                  "stop_on_failure", "health_gates", "is_enabled"]  # fmt: skip
        widgets = {
            "kind": forms.RadioSelect,
            "servers": forms.CheckboxSelectMultiple,
            "groups": forms.CheckboxSelectMultiple,
            "health_gates": forms.CheckboxSelectMultiple,
            "body": forms.Textarea(attrs={"rows": 16, "spellcheck": "false", "wrap": "off",
                                         "placeholder": "#!/usr/bin/env bash\nset -euo pipefail\n\ndf -h /"}),
            "steps": forms.Textarea(attrs={"rows": 10, "spellcheck": "false", "wrap": "off",
                                          "placeholder": "npm ci\nnpm run build\nsudo systemctl restart api  # needs its own sudo rule"}),
        }  # fmt: skip
        labels = {
            "body": "Script", "interpreter": "Run with", "app": "App name on the servers",
            "repo_url": "Git URL", "steps": "Deploy steps", "cron": "On a schedule (cron)",
            "on_push": "On every push to the branch", "is_enabled": "Enabled",
            "require_approval": "An Admin approves each automatic run",
            "batch_size": "Servers per batch", "pause_seconds": "Pause between batches (s)",
            "stop_on_failure": "Stop if a server fails",
            "health_gates": "After each batch, wait for these monitors to be up",
            "groups": "Plus every server in groups",
        }  # fmt: skip
        help_texts = {
            "cron": "minute hour day month weekday, e.g. 0 3 * * * (daily 03:00), "
                    "30 2 * * mon, or @hourly. Empty: only when started by hand.",
            "app": "Its name in `smctl app add`.",
        }  # fmt: skip

    environments = forms.MultipleChoiceField(
        choices=Environment.choices, required=False, widget=forms.CheckboxSelectMultiple,
        label="Plus every server in environments",
    )  # fmt: skip

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        instance = kwargs.get("instance")
        if instance is not None and instance.pk:
            args_ = instance.args or {}
            kwargs.setdefault("initial", {}).update(
                action=(TASK_PREFIX + args_.get("task", "")
                        if instance.action == server_tasks.ACTION else instance.action),
                arg_name=args_.get("name"), arg_lines=args_.get("lines"),
                arg_filter=args_.get("filter"), arg_command=args_.get("command"),
                rolling=instance.strategy == Strategy.ROLLING,
            )  # fmt: skip
        super().__init__(*args, **kwargs)
        servers = Server.objects.filter(account=self.account).prefetch_related("groups")
        self.fields["servers"].queryset = servers.order_by("name")
        self.fields["groups"].queryset = ServerGroup.objects.filter(account=self.account)
        self.fields["health_gates"].queryset = Monitor.objects.filter(
            account=self.account, is_enabled=True
        ).order_by("name")
        choices = action_choices(servers.filter(is_enabled=True), user)
        current = self.initial.get("action") or (self.data.get("action") if self.data else "")
        flat = {
            v for c in choices for v in ([c[0]] if isinstance(c[1], str) else [x[0] for x in c[1]])
        }
        if current and current not in flat:  # e.g. a shell schedule seen by someone without shell
            choices.append((current, current))
        self.fields["action"] = forms.ChoiceField(choices=[("", "Choose…"), *choices],
                                                  required=False, label="Action")  # fmt: skip
        for name in ("name", "description", "repo_url", "branch", "app", "cron", "batch_size",
                     "pause_seconds", "arg_name", "arg_lines", "arg_filter"):  # fmt: skip
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        for name in ("repo_url", "branch", "app", "cron"):
            self.fields[name].widget.attrs["class"] += " mono"
            self.fields[name].widget.attrs["spellcheck"] = "false"
        self.fields["action"].widget.attrs["class"] = "form-select"
        self.fields["interpreter"].widget.attrs["class"] = "form-select"
        self.fields["arg_command"].widget.attrs["class"] = "form-control mono"
        for name in ("body", "steps"):
            self.fields[name].widget.attrs["class"] = "form-control mono script-editor"
        for name in ("on_push", "require_approval", "stop_on_failure", "is_enabled", "rolling"):
            self.fields[name].widget.attrs["class"] = "form-check-input"
        self.fields["rolling"].widget.attrs["data-rolling-toggle"] = ""
        self.fields["kind"].choices = list(Kind.choices)  # no blank "choose" tile
        for name in ("batch_size", "pause_seconds"):
            self.fields[name].required = False
        self.fields["name"].widget.attrs["placeholder"] = "e.g. Nightly backup"
        self.fields["description"].widget.attrs["placeholder"] = "What it does, in a few words"
        self.fields["repo_url"].widget.attrs["placeholder"] = "git@github.com:acme/api.git"
        self.fields["branch"].widget.attrs["placeholder"] = "main"
        self.fields["app"].widget.attrs["placeholder"] = "api"
        self.fields["cron"].widget.attrs["placeholder"] = "0 3 * * *"

    # ------------------------------------------------------------------ fields

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_cron(self):
        cron = self.cleaned_data.get("cron", "").strip()
        if not cron:
            return ""
        try:
            return validate(cron)
        except CronError as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_repo_url(self):
        url = self.cleaned_data.get("repo_url", "").strip()
        if url.startswith("-") or any(c.isspace() for c in url):
            raise forms.ValidationError("That isn't a git URL.")
        return url

    def clean_body(self):
        return _norm(self.cleaned_data.get("body"))

    def clean_steps(self):
        return _norm(self.cleaned_data.get("steps"))

    # ------------------------------------------------------------------ the whole

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if kind == Kind.ACTION:
            self._clean_action(data)
        elif kind == Kind.SCRIPT:
            self._clean_script(data)
        elif kind == Kind.DEPLOY:
            for f in ("repo_url", "branch", "app"):
                if not data.get(f):
                    self.add_error(f, "Required for a git deploy.")
            if len(data.get("steps") or "") > MAX_SEND:
                self.add_error("steps", f"Deploy steps are limited to {MAX_SEND} characters.")
        if kind != Kind.DEPLOY:
            data["on_push"] = False
        has_targets = data.get("servers") or data.get("groups") or data.get("environments")
        if (data.get("cron") or data.get("on_push")) and not has_targets:
            raise forms.ValidationError(
                "Automatic runs need servers: choose servers, groups or environments."
            )
        if kind == Kind.SCRIPT and not self._may_shell():
            raise forms.ValidationError("Only Owners and Admins can save scripts.")
        return data

    def _may_shell(self) -> bool:
        return bool(self.user and self.user.has_perm("operations.run_shell"))

    def _clean_action(self, data):
        from apps.operations.services import JobError, clean_args

        action = data.get("action") or ""
        if not action:
            self.add_error("action", "Choose what to run.")
            return
        raw = {"name": data.get("arg_name"), "lines": data.get("arg_lines"),
               "filter": data.get("arg_filter"), "command": data.get("arg_command")}  # fmt: skip
        if action.startswith(TASK_PREFIX):
            action, raw = server_tasks.ACTION, {"task": action.removeprefix(TASK_PREFIX)}
        if action == "shell" and not self._may_shell():
            raise forms.ValidationError("Only Owners and Admins can save shell commands.")
        try:
            self.cleaned_args = clean_args(action, raw)
        except JobError as exc:
            raise forms.ValidationError(str(exc)) from None
        data["action"] = action

    def _clean_script(self, data):
        body = data.get("body") or ""
        if not body.strip():
            self.add_error("body", "Write the script.")
        elif len(body) > Automation.MAX_BODY:
            self.add_error("body", f"Scripts are limited to {Automation.MAX_BODY} characters.")
        else:
            probe = Automation(name=data.get("name") or "x", kind=Kind.SCRIPT, body=body,
                               interpreter=data.get("interpreter") or "bash")  # fmt: skip
            if len(probe.script_command()) > MAX_SEND:
                self.add_error("body", "Too long once quoted for its interpreter. Shorten it, "
                                       "or keep it on the server and add it as an action.")  # fmt: skip

    def save(self, commit=True):
        a = super().save(commit=False)
        d = self.cleaned_data
        a.strategy = Strategy.ROLLING if d.get("rolling") else Strategy.ALL
        a.batch_size = d.get("batch_size") or 1
        a.pause_seconds = d.get("pause_seconds") or 0
        a.environments = list(d.get("environments") or [])
        # Clear what the other kinds use, so nothing stale is sent later.
        if a.kind != Kind.ACTION:
            a.action, a.args = "", {}
        else:
            a.args = self.cleaned_args
        if a.kind != Kind.SCRIPT:
            a.body = ""
        if a.kind != Kind.DEPLOY:
            a.repo_url = a.branch = a.app = a.steps = ""
            a.on_push = False
        if a.kind == Kind.DEPLOY and not a.webhook_secret:
            from .services import new_secret

            a.webhook_secret = new_secret()
        from .services import next_run

        a.next_run_at = next_run(a)
        if commit:
            a.save()
            self.save_m2m()
        return a


class RunForm(AccountFormMixin, forms.Form):
    """Run now: optionally on servers of your choosing."""

    servers = forms.ModelMultipleChoiceField(
        queryset=Server.objects.none(), widget=forms.CheckboxSelectMultiple, required=False
    )
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, automation=None, **kwargs):
        self.automation = automation
        super().__init__(*args, **kwargs)
        self.fields["servers"].queryset = (
            Server.objects.filter(account=self.account, is_enabled=True)
            .prefetch_related("groups").order_by("name")
        )  # fmt: skip
        self.fields["note"].widget.attrs.update(
            {"class": "form-control", "placeholder": "Why, for the audit log (optional)"}
        )

    def clean(self):
        data = super().clean()
        if not data.get("servers") and not self.automation.has_targets:
            raise forms.ValidationError("Choose at least one server.")
        return data
