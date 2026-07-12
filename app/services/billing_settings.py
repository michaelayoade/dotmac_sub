"""Helpers for resolving billing settings with legacy fallbacks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.services import settings_spec

# A subscription in one of these states represents a *live* (connectable)
# service. Used for "is the service actually up" semantics.
LIVE_SERVICE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
    SubscriptionStatus.pending,
)

# Statuses we still actively COLLECT against (invoice reminders, dunning
# escalations, autopay charges). Deliberately wider than
# ``LIVE_SERVICE_STATUSES``: it adds ``blocked``, which is a *recoverable
# non-payment hold*, not a dead account — exactly the customer we most want to
# keep chasing and auto-charging so they can pay and be restored. Excluding it
# (the pre-2026-06-26 behavior) meant that the moment enforcement walled a
# non-payer, autopay/reminders/dunning could never recover them — a major
# collections leak. Only truly-terminal states stay excluded: ``stopped``
# (admin-paused), ``disabled`` (admin-terminated), ``hidden``, ``archived``,
# ``canceled`` (soft-deleted) and ``expired`` (period ended) — these must not
# keep pinging or charging the customer.
COLLECTIBLE_SERVICE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
    SubscriptionStatus.pending,
    SubscriptionStatus.blocked,
)


def billing_enabled(db: Session, *, default: bool = True) -> bool:
    """Master switch for local billing automation.

    Before DotMac Sub became the biller of record, this stayed ``false`` so the
    local runners were inert. It gates every task that
    *acts on customers* off local billing state — invoicing, autopay charges,
    dunning, prepaid enforcement, payment-arrangement checks, and subscription
    expiry — so they all activate together at cutover and none can charge,
    suspend, or expire an account before then. Resolved via ``settings_spec``
    (env fallback included) to match the invoice-cycle kill-switch.

    Single control plane: the billing MODULE (resolved by the same module
    resolver the registry uses) composes in — if the billing module is off,
    billing is off everywhere, not just in the scheduler.

    Design note: this MASTER is intentionally NOT collapsed into the
    ``billing.invoicing`` feature. Task bodies (autopay, dunning, expiry) read it
    as a cross-feature master, so equating it with invoicing would wrongly stop
    collection whenever invoice *generation* is paused. The master therefore
    stays = billing module AND the legacy ``billing.billing_enabled`` flag; the
    individual capture features (invoicing/autopay/collections/…) are gated
    independently through ``control_registry.is_enabled``.
    """
    # Local import avoids an import-time cycle (module_manager pulls settings).
    from app.services import module_manager

    if not module_manager.is_module_enabled(db, "billing"):
        return False
    value = settings_spec.resolve_value(db, SettingDomain.billing, "billing_enabled")
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def check_billing_switch(db: Session) -> dict:
    """Invariant check on the ``billing_enabled`` master switch.

    ``billing_enabled`` flipping to true unexpectedly is what let the local
    runner generate phantom invoices — a config-integrity failure, not a code
    bug, so the void cleaned the symptom, not the mechanism. This compares the
    live switch against a pinned *expected* value (``billing_enabled_expected``
    / env ``BILLING_ENABLED_EXPECTED``, default false pre-cutover). At cutover,
    set the expected value to true in the same change that enables billing.

    Returns a dict; callers should alert when ``ok`` is false.
    """
    actual = billing_enabled(db, default=False)
    # The expected value is a registered setting with the standard
    # database -> bootstrap environment -> default hierarchy.
    expected_raw = _setting_value(db, "billing_enabled_expected")
    if expected_raw is None:
        expected = False
    elif isinstance(expected_raw, bool):
        expected = expected_raw
    else:
        expected = str(expected_raw).strip().lower() in {"1", "true", "yes", "on"}
    return {"ok": actual == expected, "expected": expected, "actual": actual}


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _domain_setting_value(
    db: Session, domain: SettingDomain, key: str
) -> object | None:
    if settings_spec.get_spec(domain, key) is None:
        return settings_spec.read_stored_value(db, domain, key)
    return settings_spec.resolve_value(db, domain, key)


def _setting_value(db: Session, key: str) -> object | None:
    return _domain_setting_value(db, SettingDomain.billing, key)


def disabled_billing_components(db: Session) -> list[str]:
    """Canonical keys of billing capture-automation features that are OFF.

    Derived from the control registry (single source) — every default-ON billing
    feature (invoicing, autopay, collections, overdue-marking, arrangements,
    topup-reconciliation, …) is checked via the one resolver. Default-OFF
    opt-ins (e.g. the hourly notification runner) are excluded. Resolution is
    fail-open, so this returns only deliberately-disabled features. The hourly
    billing-switch task escalates a non-empty result to CRITICAL so no aspect of
    billing capture automation can be silently turned off while billing is live.
    """
    from app.services import control_registry

    disabled: list[str] = []
    for control in control_registry.all_controls():
        if (
            control.layer is control_registry.Layer.feature
            and control.owner_module == "billing"
            and control.on_missing  # only features that are meant to be ON
            and not control_registry.is_enabled(db, control.key)
        ):
            disabled.append(control.key)
    return disabled


def resolve_payment_due_days(
    db: Session,
    default: int = 14,
    subscriber: object | None = None,
) -> int:
    """Resolve payment due days: subscriber override > global setting > legacy keys.

    Args:
        db: Database session.
        default: Fallback if no setting is found.
        subscriber: Optional subscriber — if they have ``payment_due_days``
            set, that value takes priority over the global setting.
    """
    # Subscriber-level override takes priority
    sub_due_days = getattr(subscriber, "payment_due_days", None)
    if sub_due_days is not None:
        return max(_coerce_int(sub_due_days, default), 0)

    canonical = settings_spec.resolve_setting(
        db,
        SettingDomain.billing,
        "payment_due_days",
    )
    if canonical.source in {
        settings_spec.SettingSource.database,
        settings_spec.SettingSource.environment,
    }:
        return max(_coerce_int(canonical.value, default), 0)

    legacy_invoice = _setting_value(db, "invoice_due_days")
    if legacy_invoice is not None:
        return max(_coerce_int(legacy_invoice, default), 0)

    legacy_terms = _setting_value(db, "default_payment_terms_days")
    if legacy_terms is not None:
        return max(_coerce_int(legacy_terms, default), 0)

    return max(_coerce_int(canonical.value, default), 0)


def accounts_with_live_service(db: Session) -> set:
    """Subscriber IDs that have at least one subscription in a live service
    state (see :data:`LIVE_SERVICE_STATUSES`).

    Billing automation that *chases an existing balance* — invoice reminders,
    dunning escalations, autopay charges — must skip accounts whose services
    are all terminal: a disabled/canceled/expired service should not keep
    pinging or charging the customer. This mirrors the eligibility gate in
    ``collections.DunningWorkflow`` but spans every billing mode, since
    reminders and autopay are not postpaid-specific.
    """
    return set(
        db.scalars(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(LIVE_SERVICE_STATUSES))
            .distinct()
        ).all()
    )


def account_has_live_service(db: Session, account_id) -> bool:
    """Whether a single account still has a live (billable) service.

    Single-account counterpart to :func:`accounts_with_live_service`, for hot
    paths that already operate on one account (e.g. autopay) and only need a
    cheap existence check rather than the full set.
    """
    return (
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == account_id)
            .where(Subscription.status.in_(LIVE_SERVICE_STATUSES))
            .limit(1)
        ).first()
        is not None
    )


def accounts_with_collectible_service(db: Session) -> set:
    """Subscriber IDs with at least one subscription we still collect against
    (see :data:`COLLECTIBLE_SERVICE_STATUSES`).

    This is the gate collections automation should use — invoice reminders,
    dunning escalations, autopay charges. Unlike
    :func:`accounts_with_live_service`, it keeps ``blocked`` (recoverable
    non-payment) in scope so a walled non-payer can still be reminded and
    auto-charged back to good standing.
    """
    return set(
        db.scalars(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .distinct()
        ).all()
    )


def account_has_collectible_service(db: Session, account_id) -> bool:
    """Whether a single account still has a collectible service.

    Single-account counterpart to :func:`accounts_with_collectible_service`,
    for hot paths like autopay that operate on one account.
    """
    return (
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == account_id)
            .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .limit(1)
        ).first()
        is not None
    )
