"""4.5: single sign-on (OpenID Connect)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.users import accounts
from apps.users.models import SSOIdentity
from apps.users.sso import SSOBackend

pytestmark = pytest.mark.django_db

IDP = "https://idp.example.com"


@pytest.fixture
def sso(settings, roles, monkeypatch):
    settings.ROOT_URLCONF = "tests.sso_urls"
    settings.OIDC_ENABLED = True
    settings.OIDC_RP_CLIENT_ID, settings.OIDC_RP_CLIENT_SECRET = "dashboard", "client-secret"
    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = f"{IDP}/authorize"
    settings.OIDC_OP_TOKEN_ENDPOINT = f"{IDP}/token"
    settings.OIDC_OP_USER_ENDPOINT = f"{IDP}/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = f"{IDP}/jwks"
    settings.OIDC_ADMIN_GROUPS, settings.OIDC_OPERATOR_GROUPS = ["sre"], ["ops"]
    settings.OIDC_VIEWER_GROUPS, settings.OIDC_DEFAULT_ROLE = [], ""
    settings.AUTHENTICATION_BACKENDS = [
        *settings.AUTHENTICATION_BACKENDS,
        "apps.users.sso.SSOBackend",
    ]
    provider = {"claims": {}}
    # The provider's side of the code exchange; the library still checks state, nonce, PKCE.
    monkeypatch.setattr(
        SSOBackend, "get_token", lambda self, payload: {"id_token": "idt", "access_token": "at"}
    )
    monkeypatch.setattr(
        SSOBackend, "verify_token", lambda self, token, **kw: {"nonce": kw.get("nonce")}
    )
    monkeypatch.setattr(
        SSOBackend, "get_userinfo", lambda self, at, idt, payload: provider["claims"]
    )
    return provider


def claims(**extra):
    return {"sub": "idp|42", "email": "dana@example.com", "email_verified": True,
            "preferred_username": "dana", "groups": ["ops"], **extra}  # fmt: skip


def sign_in(client, provider, **extra):
    provider["claims"] = claims(**extra)
    start = client.get(reverse("oidc_authentication_init"))
    query = parse_qs(urlsplit(start["Location"]).query)
    return client.get(reverse("oidc_authentication_callback"),
                      {"code": "abc", "state": query["state"][0]})  # fmt: skip


def test_redirects_to_the_provider_with_state_nonce_and_pkce(client, sso):
    r = client.get(reverse("oidc_authentication_init"))
    url = urlsplit(r["Location"])
    q = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{IDP}/authorize"
    assert q["client_id"] == ["dashboard"] and q["code_challenge_method"] == ["S256"]
    assert q["state"] and q["nonce"] and q["code_challenge"]


def test_first_sign_in_creates_the_account_with_the_mapped_role(client, sso):
    r = sign_in(client, sso)
    assert r.status_code == 302 and r["Location"] == reverse("fleet:dashboard")
    user = get_user_model().objects.get(email="dana@example.com")
    assert user.username == "dana" and accounts.role_of(user) == "Operator"
    assert not user.has_usable_password() and user.sso.subject == "idp|42"
    assert client.get(reverse("fleet:dashboard")).status_code == 200
    assert {"user.sso_create", "auth.sso_login"} <= set(
        AuditLog.objects.values_list("action", flat=True)
    )


def test_role_follows_the_provider_groups(client, sso):
    sign_in(client, sso)
    client.logout()
    sign_in(client, sso, groups=["sre"])
    assert accounts.role_of(get_user_model().objects.get(email="dana@example.com")) == "Admin"


def test_no_mapped_group_or_unverified_email_is_refused(client, sso):
    for extra in ({"groups": ["marketing"]}, {"email_verified": False}, {"email_verified": None}):
        r = sign_in(client, sso, **extra)
        assert r["Location"] == "/login/?sso=failed"
    assert not get_user_model().objects.exists()
    page = client.get("/login/?sso=failed").content.decode()
    assert "Single sign-on didn't let you in" in page and "Sign in with" in page


def test_links_an_existing_account_once_and_then_follows_the_subject(client, sso, operator):
    operator.email = "dana@example.com"
    operator.save()
    sign_in(client, sso)
    assert SSOIdentity.objects.get().user == operator
    client.logout()
    sign_in(client, sso, email="dana@new-domain.example")  # email changed at the provider
    assert int(client.session["_auth_user_id"]) == operator.pk
    client.logout()
    # Managed by the provider now: the local password no longer works.
    assert not client.login(username="operator", password="pw-not-used-1!")


def test_another_subject_cannot_take_over_a_linked_account(client, sso, operator):
    operator.email = "dana@example.com"
    operator.save()
    sign_in(client, sso)
    client.logout()
    r = sign_in(client, sso, sub="attacker|1")  # same verified email, different person
    assert r["Location"] == "/login/?sso=failed" and SSOIdentity.objects.count() == 1


def test_superusers_never_sign_in_through_sso(client, sso, django_user_model):
    django_user_model.objects.create_superuser("root", "dana@example.com", "pw-not-used-1!")
    assert sign_in(client, sso, groups=["sre"])["Location"] == "/login/?sso=failed"
    assert not SSOIdentity.objects.exists() and "_auth_user_id" not in client.session


def test_sso_admins_skip_local_2fa_but_local_admins_do_not(client, sso, settings, admin_user):
    settings.REQUIRE_2FA_FOR_ADMINS = True
    sign_in(client, sso, groups=["sre"])
    assert client.get(reverse("fleet:dashboard")).status_code == 200  # the provider did MFA
    client.logout()
    client.force_login(admin_user)  # a local Admin
    assert client.get(reverse("fleet:dashboard"))["Location"] == reverse("account_2fa")


def test_new_accounts_can_be_invitation_only(client, sso, settings):
    settings.OIDC_CREATE_USERS = False
    assert sign_in(client, sso)["Location"] == "/login/?sso=failed"
    assert not get_user_model().objects.exists()
