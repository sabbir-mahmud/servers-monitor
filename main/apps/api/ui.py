"""My account → API tokens: create (shown once), list and revoke your own tokens."""

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.audit.services import record

from .models import ApiToken, Scope

EXPIRY_CHOICES = [("30", "30 days"), ("90", "90 days"), ("365", "1 year"), ("0", "Never")]


class TokenForm(forms.Form):
    name = forms.CharField(max_length=100, help_text="What uses it, e.g. Grafana or deploy-bot.")
    scope = forms.ChoiceField(choices=Scope.choices, initial=Scope.READ)
    expires = forms.ChoiceField(choices=EXPIRY_CHOICES, initial="90", label="Expires after")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not (user and user.has_perm("operations.add_job")):
            self.fields["scope"].choices = [(Scope.READ, Scope.READ.label)]
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name != "name" else "form-control"


class TokenListView(LoginRequiredMixin, TemplateView):
    template_name = "api/tokens.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("form", TokenForm(user=self.request.user))
        ctx["tokens"] = ApiToken.objects.filter(owner=self.request.user, revoked_at__isnull=True)
        return ctx

    def post(self, request):
        form = TokenForm(request.POST, user=request.user)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        days = int(form.cleaned_data["expires"])
        token, raw = ApiToken.issue(
            request.user, form.cleaned_data["name"], form.cleaned_data["scope"],
            timezone.now() + timedelta(days=days) if days else None,
        )  # fmt: skip
        record(
            request, "api_token.create", target=token.name, scope=token.scope, prefix=token.prefix
        )
        # Shown once, in this response only: never stored, never put in a redirect or session.
        return self.render_to_response(
            self.get_context_data(form=TokenForm(user=request.user), new_token=raw, created=token)
        )


class TokenRevokeView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        token = get_object_or_404(ApiToken, pk=pk, owner=request.user, revoked_at__isnull=True)
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        record(request, "api_token.revoke", target=token.name, prefix=token.prefix)
        messages.success(request, f"Revoked {token.name}. It stops working immediately.")
        return redirect("api_tokens")
