"""Moving an existing single-team install into accounts (used by migrations only).

Everything that existed before accounts goes into one Company account, and the old
global roles become memberships in it: Admin → admin (the first superuser, else the first
Admin, becomes its owner), Operator → operator, Viewer → viewer. Nobody loses access and
nothing is lost. A fresh install has no rows, so no account is created.
"""

from django.utils.text import slugify

DEFAULT_NAME = "My company"
GROUP_ROLE = {"Admin": "admin", "Operator": "operator", "Viewer": "viewer"}


def default_account(apps):
    """The account pre-account rows move into (created on first use)."""
    Account = apps.get_model("tenants", "Account")
    account = Account.objects.order_by("created_at").first()
    if account is None:
        account = Account.objects.create(
            name=DEFAULT_NAME, slug=slugify(DEFAULT_NAME), kind="company"
        )
    return account


def migrate_memberships(apps, schema_editor):
    User = apps.get_model("users", "User")
    Membership = apps.get_model("tenants", "Membership")
    users = list(User.objects.order_by("pk").prefetch_related("groups"))
    if not users:
        return
    account = default_account(apps)
    owner = next((u for u in users if u.is_superuser and u.is_active), None)
    rows = []
    for user in users:
        names = {g.name for g in user.groups.all()}
        role = next((GROUP_ROLE[n] for n in ("Admin", "Operator", "Viewer") if n in names), None)
        if user.is_superuser:
            role = "admin"
        if role is None:
            continue  # had no access before; still has none
        rows.append(Membership(account=account, user=user, role=role))
    if owner is None:
        owner = next((m.user for m in rows if m.role == "admin" and m.user.is_active), None)
    for m in rows:
        if owner is not None and m.user.pk == owner.pk:
            m.role = "owner"
    Membership.objects.bulk_create(rows, ignore_conflicts=True)
    # Old groups would grant nothing any more (permissions come from memberships), but
    # clear them so Django admin doesn't suggest otherwise.
    for user in users:
        user.groups.clear()


def backfill(app_label: str, *model_names: str):
    """A RunPython that puts every row without an account into the default account."""

    def run(apps, schema_editor):
        account = None
        for name in model_names:
            model = apps.get_model(app_label, name)
            qs = model.objects.filter(account__isnull=True)
            if qs.exists():
                account = account or default_account(apps)
                qs.update(account=account)

    return run


def backfill_alerts(apps, schema_editor):
    """Alerts take the account of their server or monitor."""
    Alert = apps.get_model("alerts", "Alert")
    for alert in Alert.objects.filter(account__isnull=True).select_related("server", "monitor"):
        target = alert.server or alert.monitor
        alert.account_id = target.account_id
        alert.save(update_fields=["account"])


def backfill_audit(apps, schema_editor):
    """Old audit rows belong to the default account so its Admins still see them."""
    AuditLog = apps.get_model("audit", "AuditLog")
    if AuditLog.objects.filter(account__isnull=True).exists():
        AuditLog.objects.filter(account__isnull=True).update(account=default_account(apps))


def backfill_tokens(apps, schema_editor):
    """API tokens act in their owner's (only) account."""
    ApiToken = apps.get_model("api", "ApiToken")
    Membership = apps.get_model("tenants", "Membership")
    for token in ApiToken.objects.filter(account__isnull=True):
        m = Membership.objects.filter(user_id=token.owner_id).order_by("created_at").first()
        token.account_id = m.account_id if m else default_account(apps).pk
        token.save(update_fields=["account"])
