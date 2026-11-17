"""Answer `user.has_perm()` from the user's role in the *current* account.

The current account is bound to the user object for the request (CurrentAccountMiddleware,
the API token check) or for a background run (`bind(user, account)` before a scheduled
job). With nothing bound, a user has no permissions at all: forgetting to bind fails
closed. Django Groups are no longer consulted, so a group left over from an old install
can't grant anything either. Superusers keep Django's built-in "every permission"; they
are the operators of the whole installation.
"""

from .models import Account, Membership

ATTR = "_membership"


class MembershipPermissionsMixin:
    """For any ModelBackend subclass: permissions come only from the bound membership."""

    def get_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        membership = getattr(user_obj, ATTR, None)
        if membership is None or not user_obj.is_active or obj is not None:
            return set()
        return set(membership.permissions)

    def get_all_permissions(self, user_obj, obj=None):
        # Not cached on the user (ModelBackend's _perm_cache): the binding can change.
        if not user_obj.is_active or user_obj.is_anonymous or obj is not None:
            return set()
        return self.get_group_permissions(user_obj)


def bind(user, membership: Membership | None) -> None:
    setattr(user, ATTR, membership)
    for cache in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        if hasattr(user, cache):
            delattr(user, cache)


def bind_account(user, account: Account | None) -> Membership | None:
    """Bind `user`'s membership in `account` (None if they aren't a member any more)."""
    membership = None
    if user is not None and account is not None and getattr(user, "pk", None):
        membership = (
            Membership.objects.select_related("account").filter(user=user, account=account).first()
        )
    if user is not None:
        bind(user, membership)
    return membership


def membership_of(user) -> Membership | None:
    return getattr(user, ATTR, None)
