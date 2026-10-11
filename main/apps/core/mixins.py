from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponse
from django.shortcuts import resolve_url


class StaffViewMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Every dashboard view starts here: anonymous → login, signed in without perm → 403."""

    permission_denied_message = "You do not have permission to do that."
    raise_exception = False

    def handle_no_permission(self):
        # Signed-in users without the permission get a 403, not a login loop.
        if self.request.user.is_authenticated:
            self.raise_exception = True
        elif getattr(self.request, "htmx", False):
            # An expired session during HTMX polling would otherwise swap the login page
            # into a table cell. Ask htmx to navigate the whole window instead.
            response = HttpResponse(status=204)
            response["HX-Redirect"] = resolve_url(settings.LOGIN_URL)
            return response
        return super().handle_no_permission()


class HtmxTemplateMixin:
    """Render a named template partial for HTMX requests (Django 6 `{% partialdef %}`)."""

    htmx_partial: str | None = None

    def get_template_names(self):
        names = super().get_template_names()
        if self.htmx_partial and getattr(self.request, "htmx", False):
            return [f"{names[0]}#{self.htmx_partial}"]
        return names
