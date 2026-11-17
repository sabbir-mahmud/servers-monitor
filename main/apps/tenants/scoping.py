"""Keeping every query inside the current account.

Rules (tests/test_isolation.py checks them for every page and API endpoint):

- Views use `AccountScopedMixin` (CBVs with a model) or `scoped(request, Model)` /
  `get_scoped_or_404(...)`. Never `Model.objects` directly with an id from the URL.
- Forms use `AccountFormMixin`: every model choice field whose model belongs to an
  account is narrowed to the current one, so a POSTed id from another account is simply
  "not a valid choice". New objects get the account set before validation, so per-account
  unique names are checked by the form, not left to an IntegrityError.
- Background work (relay, checker, scheduler) is global by design and carries the
  account along from the object it works on (server.account, monitor.account…).
"""

from django.core.exceptions import FieldDoesNotExist
from django.forms import ModelChoiceField
from django.http import Http404
from django.shortcuts import get_object_or_404


def has_account_field(model) -> bool:
    try:
        model._meta.get_field("account")
    except FieldDoesNotExist:
        return False
    return True


def account_lookup(model) -> str | None:
    """The filter that ties `model` to an account: directly, or through its parent."""
    if has_account_field(model):
        return "account"
    return getattr(model, "account_path", None)


def scope(queryset, account):
    lookup = account_lookup(queryset.model)
    if lookup is None:
        raise TypeError(f"{queryset.model.__name__} has no account to scope by")
    if account is None:
        return queryset.none()
    return queryset.filter(**{lookup: account})


def scoped(request, model_or_queryset):
    qs = model_or_queryset
    if not hasattr(qs, "filter"):
        qs = qs._default_manager.all()
    return scope(qs, getattr(request, "account", None))


def get_scoped_or_404(request, model_or_queryset, **lookup):
    return get_object_or_404(scoped(request, model_or_queryset), **lookup)


def require_account(request):
    if getattr(request, "account", None) is None:
        raise Http404
    return request.account


class AccountScopedMixin:
    """CBV mixin: querysets narrowed to request.account, forms told the account, and
    new objects created in it."""

    def get_queryset(self):
        return scope(super().get_queryset(), getattr(self.request, "account", None))

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if issubclass(self.get_form_class(), AccountFormMixin):
            kwargs["account"] = self.request.account
        return kwargs


class AccountFormMixin:
    """Form mixin taking `account=`. Must come before forms.(Model)Form in the bases."""

    def __init__(self, *args, account=None, **kwargs):
        self.account = account
        super().__init__(*args, **kwargs)
        instance = getattr(self, "instance", None)
        if instance is not None and has_account_field(type(instance)):
            if instance._state.adding and account is not None:
                instance.account = account
        self.scope_choices()

    def scope_choices(self):
        """Call again after replacing a field's queryset in a subclass __init__."""
        for field in self.fields.values():
            if isinstance(field, ModelChoiceField) and field.queryset is not None:
                if account_lookup(field.queryset.model):
                    field.queryset = scope(field.queryset, self.account)

    def _get_validation_exclusions(self):
        # Validate per-account unique constraints even though `account` isn't a field.
        exclude = super()._get_validation_exclusions()
        exclude.discard("account")
        return exclude
