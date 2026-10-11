"""Single sign-on with any OpenID Connect provider (Keycloak, Authentik, Google, Entra ID…).

mozilla-django-oidc runs the authorization-code flow (state, nonce, PKCE, signature and
audience checks); this backend decides who gets in and with which role:

- People are matched by the provider's stable subject (`sub`), stored on first sign-in.
  A first sign-in links to an existing account only through a **verified** email address.
- Superusers never sign in through SSO: they're the local break-glass accounts.
- The role comes from a group claim (OIDC_ADMIN_GROUPS, …) on every sign-in, so removing
  someone from a group at the provider takes effect the next time they sign in. Matching
  no group means no entry, unless OIDC_DEFAULT_ROLE is set.
- Two-factor is the provider's job for SSO sessions; local Admin sign-ins still need it.
"""

import logging
import re

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, get_user_model
from django.core.exceptions import SuspiciousOperation
from django.db import IntegrityError, transaction
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from apps.audit.services import record

from . import accounts
from .models import SSOIdentity

logger = logging.getLogger(__name__)

BACKEND = "apps.users.sso.SSOBackend"


def is_sso_session(request) -> bool:
    return request.session.get(BACKEND_SESSION_KEY) == BACKEND


def _groups(claims) -> set[str]:
    raw = claims.get(settings.OIDC_GROUPS_CLAIM) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(g) for g in raw if isinstance(g, str | int)}


def role_for(claims) -> str | None:
    groups = _groups(claims)
    for role, wanted in (("Admin", settings.OIDC_ADMIN_GROUPS),
                         ("Operator", settings.OIDC_OPERATOR_GROUPS),
                         ("Viewer", settings.OIDC_VIEWER_GROUPS)):  # fmt: skip
        if groups & set(wanted):
            return role
    return settings.OIDC_DEFAULT_ROLE or None


def username_for(email, claims=None) -> str:
    """A readable, unique username: preferred_username, else the email's local part."""
    claims = claims or {}
    base = str(claims.get("preferred_username") or (email or "").split("@")[0] or "user")
    base = re.sub(r"[^\w.@+-]", "", base)[:140] or "user"
    User = get_user_model()
    name, n = base, 1
    while User.objects.filter(username__iexact=name).exists():
        n += 1
        name = f"{base}{n}"
    return name


class SSOBackend(OIDCAuthenticationBackend):
    def verify_claims(self, claims) -> bool:
        if not claims.get("sub") or not claims.get("email"):
            return False
        if claims.get("email_verified") is not True and not settings.OIDC_ALLOW_UNVERIFIED_EMAIL:
            logger.warning(
                "SSO refused %s: the provider didn't verify that email", claims.get("email")
            )
            return False
        if role_for(claims) is None:
            logger.warning("SSO refused %s: in no group mapped to a role", claims.get("email"))
            return False
        return True

    def filter_users_by_claims(self, claims):
        User = get_user_model()
        linked = User.objects.filter(sso__subject=str(claims["sub"]))
        if linked.exists():
            return linked
        # First SSO sign-in for this subject: link an existing (non-superuser) account by its
        # verified email, if there is exactly one.
        return User.objects.filter(email__iexact=claims["email"], sso__isnull=True)

    def create_user(self, claims):
        if not settings.OIDC_CREATE_USERS:
            logger.warning(
                "SSO refused %s: no account, and OIDC_CREATE_USERS is off", claims.get("email")
            )
            return None
        User = get_user_model()
        if User.objects.filter(email__iexact=claims["email"]).exists():
            logger.warning("SSO refused %s: that email belongs to an account linked to another "
                           "provider subject", claims["email"])  # fmt: skip
            return None
        user = User.objects.create_user(username_for(claims["email"], claims), email=claims["email"],
                                        first_name=str(claims.get("given_name") or "")[:150],
                                        last_name=str(claims.get("family_name") or "")[:150])  # fmt: skip
        user.set_unusable_password()  # signs in through the provider only
        user.save(update_fields=["password"])
        record(user, "user.sso_create", target=user.get_username(), email=user.email)
        return self.update_user(user, claims)

    @transaction.atomic
    def update_user(self, user, claims):
        if user.is_superuser:
            raise SuspiciousOperation("superusers sign in with their local password, not SSO")
        try:
            SSOIdentity.objects.get_or_create(user=user, defaults={"subject": str(claims["sub"])})
        except IntegrityError:
            raise SuspiciousOperation("this provider account is linked to another user") from None
        if user.sso.subject != str(claims["sub"]):
            raise SuspiciousOperation("this account is linked to a different provider account")
        role = role_for(claims)
        if role != accounts.role_of(user):
            try:
                accounts.set_role(user, role)
                record(user, "user.sso_role", target=user.get_username(), role=role)
            except accounts.AccountError as exc:  # e.g. demoting the last Admin
                logger.warning("SSO kept %s's role: %s", user.get_username(), exc)
        return user

    def get_or_create_user(self, access_token, id_token, payload):
        user = super().get_or_create_user(access_token, id_token, payload)
        if user is not None:
            record(user, "auth.sso_login", target=user.get_username())
        return user
