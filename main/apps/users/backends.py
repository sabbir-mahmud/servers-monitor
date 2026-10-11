from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    """ModelBackend that also accepts an email address in the username box.

    Emails are unique case-insensitively (see User.Meta), so at most one account matches.
    Inactive accounts (deactivated, or awaiting approval) are refused as usual.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        User = get_user_model()
        lookup = Q(username__iexact=username)
        if "@" in username:
            lookup |= Q(email__iexact=username)
        user = User.objects.filter(lookup).order_by("pk").first()
        if user is None:
            # Run the hasher anyway so a missing account takes as long as a wrong password.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            if hasattr(user, "sso") and not user.is_superuser:
                # Managed by the SSO provider: removing someone there must lock them out.
                return None
            return user
        return None
