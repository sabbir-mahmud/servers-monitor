from django.contrib.auth import views as auth_views
from django.core.exceptions import ValidationError

from apps.core.utils import client_ip

from . import throttle


class ThrottledLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if throttle.is_locked(username, client_ip(request)):
            form = self.get_form()
            form.add_error(None, ValidationError("Too many failed attempts. Try again later."))
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        if self.request.method == "POST":
            throttle.record_failure(self.request.POST.get("username", ""), client_ip(self.request))
        return super().form_invalid(form)

    def form_valid(self, form):
        throttle.clear(form.get_user().get_username(), client_ip(self.request))
        return super().form_valid(form)


class LogoutView(auth_views.LogoutView):
    pass
