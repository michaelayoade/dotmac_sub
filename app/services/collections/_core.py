import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    DunningAction,
    PolicyDunningStep,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningActionLog, DunningCase, DunningCaseStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.collections import (
    BillingEnforcementRunRequest,
    BillingEnforcementRunResponse,
    DunningActionLogCreate,
    DunningActionLogUpdate,
    DunningCaseCreate,
    DunningCaseUpdate,
    DunningRunRequest,
    DunningRunResponse,
)
from app.services import enforcement_window, settings_spec
from app.services.billing.invoice_classification import collectible_ar_invoice_filter
from app.services.billing_prepaid_overlap_repair import (
    apply_prepaid_overlap_hold,
    invoice_paid_prepaid_overlap,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


def _resolve_positive_int_setting(
    db: Session,
    domain: SettingDomain,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = settings_spec.resolve_value(db, domain, key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _suspension_notification_dedupe_hours(db: Session) -> int:
    return _resolve_positive_int_setting(
        db,
        SettingDomain.collections,
        "suspension_notification_dedupe_hours",
        24,
        minimum=1,
        maximum=168,
    )


def _resolve_prepaid_available_balance(db: Session, account_id: str) -> Decimal:
    """Authoritative prepaid available balance from customer financial events.

    Enforcement uses the same canonical credits and debits that statements show:
    real payments, real service charges, real credit notes/refunds, real approved
    adjustments, and legacy mirrored transactions. Internal repair artifacts are
    excluded before this balance is calculated.
    """
    from app.services.customer_financial_ledger import list_customer_financial_events

    balances_by_currency: dict[str, Decimal] = {}
    for event in list_customer_financial_events(db, account_id, currency=None):
        currency = event.currency or "NGN"
        balances_by_currency[currency] = (
            balances_by_currency.get(currency, Decimal("0.00")) + event.signed_amount
        )
    balances = list(balances_by_currency.values())
    return min(balances) if balances else Decimal("0.00")


def get_available_balance(db: Session, account_id: str) -> Decimal:
    """Return the available account balance visible to customer billing flows."""
    return _resolve_prepaid_available_balance(db, account_id)


def has_overdue_balance(db: Session, account_id: str) -> bool:
    """Return True if the account still owes money on a past-due invoice.

    An invoice counts as overdue debt when it is active, retains a
    ``balance_due > 0``, and is either already marked ``overdue`` or is an
    ``issued``/``partially_paid`` invoice whose ``due_at`` has elapsed (the
    hourly overdue sweep may not have flipped its status yet).

    Used as the restore guard for overdue suspensions: a partial payment
    must not lift the suspension while overdue debt remains.
    """
    now = datetime.now(UTC)
    invoices = (
        db.query(Invoice.id)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.balance_due > 0)
        .filter(collectible_ar_invoice_filter())
        .filter(
            or_(
                Invoice.status == InvoiceStatus.overdue,
                and_(
                    Invoice.status.in_(
                        [InvoiceStatus.issued, InvoiceStatus.partially_paid]
                    ),
                    Invoice.due_at.is_not(None),
                    Invoice.due_at <= now,
                ),
            )
        )
        .all()
    )
    for (invoice_id,) in invoices:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            continue
        if (invoice.metadata_ or {}).get("reconciliation_hold"):
            continue
        return True
    return False


def _effective_billing_mode_for_account(
    db: Session, account: Subscriber
) -> BillingMode | None:
    """Resolve billing mode from collectible services, falling back to account."""
    modes = {
        row[0]
        for row in (
            db.query(Subscription.billing_mode)
            .filter(Subscription.subscriber_id == account.id)
            .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .distinct()
            .all()
        )
        if row[0] is not None
    }
    if BillingMode.prepaid in modes:
        effective = BillingMode.prepaid
    elif BillingMode.postpaid in modes:
        effective = BillingMode.postpaid
    else:
        effective = account.billing_mode
    if modes and account.billing_mode != effective:
        logger.info(
            "Resolved billing mode for account %s from collectible subscriptions: "
            "account=%s effective=%s",
            account.id,
            account.billing_mode.value if account.billing_mode else None,
            effective.value if effective else None,
        )
    return effective


def _general_default_policy_set_id(db: Session, account: Subscriber):
    """The general (fallback) dunning policy for an account's billing mode.

    Configured via the collections settings ``default_prepaid_policy_set_id`` /
    ``default_postpaid_policy_set_id`` (seeded to the immediate-suspend prepaid
    policy and the 30-day postpaid policy respectively).
    """
    billing_mode = _effective_billing_mode_for_account(db, account)
    key = (
        "default_prepaid_policy_set_id"
        if billing_mode == BillingMode.prepaid
        else "default_postpaid_policy_set_id"
    )
    # Read the setting row directly: these are seeded data settings, not declared
    # in SETTINGS_SPECS, so settings_spec.resolve_value() would ignore them.
    raw = (
        db.query(DomainSetting.value_text)
        .filter(DomainSetting.domain == SettingDomain.collections)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .scalar()
    )
    if not raw:
        return None
    try:
        return coerce_uuid(str(raw))
    except (ValueError, TypeError):
        return None


def _resolve_policy_set_for_account(db: Session, account_id: str):
    """Resolve the dunning policy for an account, most specific override first:

    1. account override   (subscriber.policy_set_id)
    2. reseller override   (reseller.policy_set_id)
    3. offer / offer_version policy_set_id
    4. general default by billing mode (collections setting)
    """
    account = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(account_id)))
    if account is None:
        return None
    # 1. account-level override
    if account.policy_set_id:
        return account.policy_set_id
    # 2. reseller-level override
    if account.reseller_id:
        reseller = db.get(Reseller, account.reseller_id)
        if reseller and reseller.policy_set_id:
            return reseller.policy_set_id
    # 3. offer / offer_version assignment
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account_id)
        .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .options(
            selectinload(Subscription.offer_version),
            selectinload(Subscription.offer),
        )
        .all()
    )
    priority = {
        SubscriptionStatus.active: 0,
        SubscriptionStatus.suspended: 1,
        SubscriptionStatus.pending: 2,
        SubscriptionStatus.blocked: 3,
    }
    subscriptions.sort(
        key=lambda sub: (
            priority.get(sub.status, 99),
            -(sub.created_at.timestamp() if sub.created_at else 0),
        )
    )
    for subscription in subscriptions:
        if subscription.offer_version and subscription.offer_version.policy_set_id:
            return subscription.offer_version.policy_set_id
        if subscription.offer and subscription.offer.policy_set_id:
            return subscription.offer.policy_set_id
    # 4. general default by billing mode
    return _general_default_policy_set_id(db, account)


def _resolve_dunning_steps(db: Session, policy_set_id: str):
    return (
        db.query(PolicyDunningStep)
        .filter(PolicyDunningStep.policy_set_id == policy_set_id)
        .order_by(PolicyDunningStep.day_offset.asc())
        .all()
    )


def _resolve_overdue_days(
    invoice: Invoice,
    run_at: datetime,
    account: Subscriber | None = None,
    db: Session | None = None,
) -> int:
    """Calculate days overdue, accounting for account grace period.

    Args:
        invoice: The invoice to check
        run_at: The reference datetime for calculating overdue
        account: The subscriber account (optional, for grace period)

    Returns:
        Number of days overdue (after grace period), minimum 0
    """
    if not invoice.due_at:
        return 0
    delta = run_at.date() - invoice.due_at.date()
    raw_days = max(delta.days, 0)

    # Subtract the account grace period. If a migrated account has no explicit
    # value, fall back to the billing-mode default so dunning can be governed
    # centrally instead of cutting immediately on null account data.
    grace_period = 0
    if account and account.grace_period_days is not None:
        grace_period = int(account.grace_period_days)
    elif account and db is not None:
        billing_mode = _effective_billing_mode_for_account(db, account)
        setting_key = (
            "prepaid_default_grace_period_days"
            if billing_mode == BillingMode.prepaid
            else "postpaid_default_grace_period_days"
        )
        value = settings_spec.resolve_value(db, SettingDomain.billing, setting_key)
        try:
            grace_period = int(str(value or 0))
        except (TypeError, ValueError):
            grace_period = 0

    return max(raw_days - grace_period, 0)


def _create_action_log(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    step_day: int | None,
    invoice_id: str | None,
    outcome: str | None = None,
    notes: str | None = None,
):
    log = DunningActionLog(
        case_id=case.id,
        invoice_id=invoice_id,
        step_day=step_day,
        action=action,
        outcome=outcome,
        notes=notes,
    )
    db.add(log)
    return log


def _refresh_account_status(db: Session, account_id) -> None:
    """Keep subscriber status aligned with dunning case state."""
    from app.services.account_lifecycle import compute_account_status

    try:
        compute_account_status(db, str(account_id))
    except ValueError:
        logger.warning(
            "Cannot recompute account status for %s: account not found", account_id
        )


def _account_has_dedicated_bundle(db: Session, account_id) -> bool:
    """True if any of the account's subscriptions is in a dedicated bundle.

    Dedicated internet (``plan_family='dedicated'``) is contract/SLA-managed and
    must never be auto-suspended. Because a bundle shares one subscriber-level
    RADIUS identity, a single dedicated member makes the whole account hands-off.
    """
    from app.models.catalog import SubscriptionBundle

    return (
        db.scalar(
            select(SubscriptionBundle.id)
            .join(Subscription, Subscription.bundle_id == SubscriptionBundle.id)
            .where(
                Subscription.subscriber_id == coerce_uuid(account_id),
                SubscriptionBundle.is_dedicated.is_(True),
            )
            .limit(1)
        )
        is not None
    )


def _suspend_account(
    db: Session,
    account_id: str,
    reason: EnforcementReason = EnforcementReason.overdue,
    source: str = "dunning",
) -> bool:
    """Suspend account via enforcement locks on its active subscriptions.

    Delegates to ``account_lifecycle.suspend_subscription`` per subscription
    and lets ``compute_account_status`` derive the subscriber status.

    Unified dunning suspends the whole account on collectible arrears after the
    live balance/shield gates pass.

    Returns True if any subscription was suspended, False otherwise.
    """
    from app.services.account_lifecycle import suspend_subscription

    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account:
        logger.warning("Cannot suspend account %s: account not found", account_id)
        return False

    if account.status == SubscriberStatus.canceled:
        logger.info("Account %s is canceled, skipping suspension", account_id)
        return False

    if _account_has_dedicated_bundle(db, account.id):
        logger.info(
            "dedicated_bundle_skip: account %s has a dedicated-internet bundle; "
            "hands-off for auto-enforcement",
            account_id,
        )
        return False

    query = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account.id)
        .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
    )
    subscriptions = query.all()
    suspended_count = 0
    for sub in subscriptions:
        try:
            suspend_subscription(
                db,
                str(sub.id),
                reason=reason,
                source=source,
            )
            suspended_count += 1
        except ValueError as e:
            if "Cannot suspend" in str(e):
                logger.info("Skipped suspending subscription %s: %s", sub.id, e)
            else:
                logger.error("Failed to suspend subscription %s: %s", sub.id, e)
                raise

    if suspended_count:
        emit_event(
            db,
            EventType.subscriber_suspended,
            {
                "account_id": str(account.id),
                "subscriber_id": str(account.id),
                "suspended_subscriptions": suspended_count,
            },
            account_id=account.id,
            subscriber_id=account.id,
        )

    logger.info(
        "Suspended account %s with %d subscriptions", account_id, suspended_count
    )
    return suspended_count > 0


def _restore_account(
    db: Session,
    account_id: str,
    trigger: str = "payment",
    resolved_by: str | None = None,
) -> int:
    """Restore account subscriptions via enforcement lock resolution.

    Delegates to ``account_lifecycle.restore_subscription`` per subscription.
    Only locks whose reason allows the given trigger will be resolved.
    Subscriptions with remaining locks from other reasons stay suspended.

    Returns count of subscriptions actually restored to active.
    """
    from app.services.account_lifecycle import restore_subscription

    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account:
        logger.warning("Cannot restore account %s: account not found", account_id)
        return 0
    if account.status == SubscriberStatus.canceled:
        logger.info("Account %s is canceled, skipping restore", account_id)
        return 0

    resolved_by_str = resolved_by or f"{trigger}:{account_id}"
    now = datetime.now(UTC)

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account.id)
        .filter(
            Subscription.status.in_(
                (SubscriptionStatus.suspended, SubscriptionStatus.blocked)
            )
        )
        .all()
    )
    restored_count = 0
    for sub in subscriptions:
        if sub.end_at and sub.end_at <= now:
            continue
        try:
            restored = restore_subscription(
                db,
                str(sub.id),
                trigger=trigger,
                resolved_by=resolved_by_str,
            )
            if restored:
                restored_count += 1
        except ValueError as e:
            logger.error(
                "Failed to restore subscription %s for account %s: %s",
                sub.id,
                account_id,
                e,
            )
        except Exception as e:
            logger.error(
                "Unexpected error restoring subscription %s for account %s: %s",
                sub.id,
                account_id,
                e,
            )

    if restored_count:
        emit_event(
            db,
            EventType.subscriber_reactivated,
            {
                "account_id": str(account.id),
                "subscriber_id": str(account.id),
                "restored_subscriptions": restored_count,
            },
            account_id=account.id,
            subscriber_id=account.id,
        )
        logger.info(
            "Restored %d subscriptions for account %s", restored_count, account_id
        )
    return restored_count


def _get_account_email(db: Session, account_id: str) -> str | None:
    """Get the billing email for an account."""
    account = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(account_id)))
    if not account:
        return None
    return str(account.email) if account.email else None


def _throttle_account(db: Session, account_id: str) -> tuple[bool, int]:
    """Apply throttle RADIUS profile to account's access credentials.

    Throttling reduces bandwidth for the subscriber without fully suspending
    service. This requires a 'throttle' RADIUS profile to be configured.

    Args:
        db: Database session
        account_id: The account to throttle

    Returns:
        Tuple of (success: bool, credentials_throttled: int)
    """
    # Get throttle profile ID from settings
    throttle_profile_id = settings_spec.resolve_value(
        db, SettingDomain.collections, "throttle_radius_profile_id"
    )
    if not throttle_profile_id:
        logger.warning(
            f"Cannot throttle account {account_id}: throttle_radius_profile_id not configured"
        )
        return False, 0

    # Verify the throttle profile exists
    throttle_profile = db.get(RadiusProfile, throttle_profile_id)
    if not throttle_profile or not throttle_profile.is_active:
        logger.warning(
            f"Cannot throttle account {account_id}: throttle profile {throttle_profile_id} not found or inactive"
        )
        return False, 0

    # Get all active access credentials for the account
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )

    if not credentials:
        logger.info(f"No active credentials to throttle for account {account_id}")
        return True, 0

    throttled_count = 0
    original_profiles = {}  # Track original profiles for logging
    for cred in credentials:
        # Track original profile for audit/logging purposes
        if cred.radius_profile_id and str(cred.radius_profile_id) != str(
            throttle_profile_id
        ):
            original_profiles[str(cred.id)] = str(cred.radius_profile_id)
        cred.radius_profile_id = throttle_profile.id
        throttled_count += 1

    # Log original profiles for debugging (can be used for manual recovery if needed)
    if original_profiles:
        logger.info(
            f"Throttled credentials for account {account_id}, "
            f"original profiles: {original_profiles}"
        )

    # Emit throttle event
    emit_event(
        db,
        EventType.subscriber_throttled,
        {
            "account_id": str(account_id),
            "credentials_throttled": throttled_count,
            "throttle_profile_id": str(throttle_profile_id),
        },
        account_id=coerce_uuid(account_id),
    )

    logger.info(f"Throttled {throttled_count} credentials for account {account_id}")
    return True, throttled_count


def _restore_throttle(db: Session, account_id: str) -> int:
    """Remove throttle and restore original RADIUS profiles.

    When a throttled account makes payment, restore their original
    bandwidth by removing the throttle profile.

    Args:
        db: Database session
        account_id: The account to restore

    Returns:
        Number of credentials restored
    """
    throttle_profile_id = settings_spec.resolve_value(
        db, SettingDomain.collections, "throttle_radius_profile_id"
    )
    if not throttle_profile_id:
        return 0

    # Get credentials with throttle profile
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.radius_profile_id == coerce_uuid(throttle_profile_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )

    if not credentials:
        return 0

    restored_count = 0
    for cred in credentials:
        # Get default profile from subscription's offer
        subscription = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == coerce_uuid(account_id))
            .filter(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.suspended]
                )
            )
            .first()
        )
        if subscription and subscription.offer:
            # Get the offer's RADIUS profile
            from app.models.catalog import OfferRadiusProfile

            offer_profile = (
                db.query(OfferRadiusProfile)
                .filter(OfferRadiusProfile.offer_id == subscription.offer_id)
                .first()
            )
            if offer_profile:
                cred.radius_profile_id = offer_profile.profile_id
            else:
                cred.radius_profile_id = None
        else:
            cred.radius_profile_id = None
        restored_count += 1

    if restored_count:
        logger.info(
            f"Restored {restored_count} throttled credentials for account {account_id}"
        )

    return restored_count


def _create_throttle_notification(
    db: Session, account_id: str, days_overdue: int
) -> None:
    """Create email notification that account has been throttled."""
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create throttle notification for account {account_id}: no email found"
        )
        return

    notification = Notification(
        channel=NotificationChannel.email,
        event_type="account_throttled",
        category="billing",
        recipient=email,
        subject="Service Speed Reduced - Payment Overdue",
        body=f"Your internet speed has been reduced due to payment being {days_overdue} days overdue. "
        "Please make a payment to restore full speed.",
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created throttle notification for account {account_id}")


def _create_suspension_warning_notification(
    db: Session,
    account_id: str,
    days_overdue: int,
    note: str | None = None,
    invoice_id: str | None = None,
) -> None:
    """Emit a suspension warning event so notification policy owns delivery."""
    invoice = db.get(Invoice, coerce_uuid(invoice_id)) if invoice_id else None
    emit_event(
        db,
        EventType.subscription_suspension_warning,
        {
            "invoice_id": str(invoice.id) if invoice else (invoice_id or ""),
            "invoice_number": invoice.invoice_number if invoice else "",
            "amount": str(invoice.balance_due or invoice.total or 0)
            if invoice
            else "0.00",
            "days_overdue": str(days_overdue),
            "grace_hours": "0",
            "reason": "dunning",
            "note": note or "",
        },
        account_id=coerce_uuid(account_id),
    )
    logger.info("Emitted suspension warning event for account %s", account_id)


def _create_suspension_notification(db: Session, account_id: str) -> None:
    """Create email notification that account has been suspended."""
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create suspension notification for account {account_id}: no email found"
        )
        return

    # Idempotency check: don't create duplicate suspension notification within the
    # configured suppression window.
    recent_threshold = datetime.now(UTC) - timedelta(
        hours=_suspension_notification_dedupe_hours(db)
    )
    existing = (
        db.query(Notification)
        .filter(Notification.recipient == email)
        .filter(Notification.subject == "Account Suspended")
        .filter(Notification.created_at > recent_threshold)
        .filter(Notification.is_active.is_(True))
        .first()
    )
    if existing:
        logger.debug(
            f"Skipping suspension notification for account {account_id}: recent notification exists"
        )
        return

    notification = Notification(
        channel=NotificationChannel.email,
        event_type="account_suspended",
        category="billing",
        recipient=email,
        subject="Account Suspended",
        body="Your account has been suspended due to non-payment. Please make a payment to restore service.",
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created suspension notification for account {account_id}")


def _dunning_shield_reason(db: Session, account_id) -> str | None:
    """Return why dunning enforcement should be skipped, or None.

    Mirrors the event-driven overdue path (``EnforcementHandler.
    _suspension_shield_reason``) so the two enforcement systems agree: a
    customer with an admin-approved payment arrangement or a bank-transfer
    proof under review must NOT be dunned/suspended. The scheduled dunning
    runner previously ignored this shield entirely.
    """
    from app.models.payment_arrangement import (
        ArrangementStatus,
        PaymentArrangement,
    )
    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    arrangement_id = (
        db.query(PaymentArrangement.id)
        .filter(PaymentArrangement.subscriber_id == account_id)
        .filter(PaymentArrangement.status == ArrangementStatus.active)
        .filter(PaymentArrangement.is_active.is_(True))
        .limit(1)
        .scalar()
    )
    if arrangement_id:
        return f"active payment arrangement {arrangement_id}"
    proof_id = (
        db.query(PaymentProof.id)
        .filter(PaymentProof.account_id == account_id)
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .limit(1)
        .scalar()
    )
    if proof_id:
        return f"payment proof {proof_id} pending review"
    from app.services.service_extensions import extension_shield_reason

    return extension_shield_reason(db, account_id)


def _bulk_dunning_shield_reasons(
    db: Session, account_ids: list[UUID] | set[UUID]
) -> dict[UUID, str]:
    """Return account shield reasons for a dunning cohort in bulk."""
    if not account_ids:
        return {}
    ids = {coerce_uuid(str(account_id)) for account_id in account_ids}
    from app.models.payment_arrangement import (
        ArrangementStatus,
        PaymentArrangement,
    )
    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    reasons: dict[UUID, str] = {}
    arrangement_rows = (
        db.query(PaymentArrangement.subscriber_id, PaymentArrangement.id)
        .filter(PaymentArrangement.subscriber_id.in_(ids))
        .filter(PaymentArrangement.status == ArrangementStatus.active)
        .filter(PaymentArrangement.is_active.is_(True))
        .all()
    )
    for account_id, arrangement_id in arrangement_rows:
        reasons.setdefault(account_id, f"active payment arrangement {arrangement_id}")

    proof_rows = (
        db.query(PaymentProof.account_id, PaymentProof.id)
        .filter(PaymentProof.account_id.in_(ids))
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .all()
    )
    for account_id, proof_id in proof_rows:
        reasons.setdefault(account_id, f"payment proof {proof_id} pending review")

    from app.services.service_extensions import bulk_extension_shield_reasons

    for account_id, reason in bulk_extension_shield_reasons(db, ids).items():
        reasons.setdefault(account_id, reason)
    return reasons


# Dunning actions that enforce against the account (and so must re-check the
# live balance + shield right before acting, not trust the run's snapshot).
_ENFORCING_ACTIONS = frozenset(
    {DunningAction.suspend, DunningAction.reject, DunningAction.throttle}
)
_NON_ADVANCING_DUNNING_OUTCOMES = frozenset(
    {
        "balance_cleared",
        "shielded",
        "prepaid_balance_available",
        "notice_grace_active",
        "enforcement_health_blocked",
    }
)


def _account_has_prepaid_service(db: Session, account: Subscriber) -> bool:
    if account.billing_mode == BillingMode.prepaid:
        return True
    return (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == account.id)
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(
            Subscription.status.in_(
                [
                    SubscriptionStatus.active,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.pending,
                ]
            )
        )
        .limit(1)
        .first()
        is not None
    )


def _prepaid_balance_gate_skip_reason(db: Session, account: Subscriber) -> str | None:
    """Return why prepaid enforcement should not cut service, or None.

    Prepaid service cuts are guarded by local available balance, not by prepaid
    invoice rows. Ledger credit that covers the account prevents suspension
    even if a legacy prepaid invoice row is still technically past due.
    """
    if not _account_has_prepaid_service(db, account):
        return None

    default_threshold = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_default_min_balance"
    )
    threshold_value = (
        account.min_balance
        if account.min_balance is not None
        else (default_threshold if default_threshold is not None else "0.00")
    )
    threshold = Decimal(str(threshold_value))
    balance = _resolve_prepaid_available_balance(db, str(account.id))
    if balance >= threshold:
        logger.info(
            "Dunning enforcement skipped for prepaid account %s: "
            "available balance %s >= threshold %s",
            account.id,
            balance,
            threshold,
        )
        return "prepaid_balance_available"
    return None


def _minimum_enforcement_age_skip_reason(
    db: Session, account: Subscriber, overdue_days: int
) -> str | None:
    """Block service-affecting action until the notice runway has elapsed."""
    if _effective_billing_mode_for_account(db, account) == BillingMode.prepaid:
        return None
    value = settings_spec.resolve_value(
        db,
        SettingDomain.collections,
        "billing_enforcement_min_enforcing_day_offset",
    )
    try:
        minimum_days = int(str(value if value is not None else 3))
    except (TypeError, ValueError):
        minimum_days = 3
    if minimum_days <= 0:
        return None
    if overdue_days < minimum_days:
        logger.info(
            "Dunning enforcement skipped for account %s: overdue_days %s < "
            "minimum enforcing day %s",
            account.id,
            overdue_days,
            minimum_days,
        )
        return "notice_grace_active"
    return None


def _execute_dunning_action(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    day_offset: int,
    note: str | None,
    overdue_days: int | None = None,
    invoice_id: str | None = None,
) -> str:
    """Execute a dunning action and return the outcome.

    Args:
        db: Database session
        case: The dunning case
        action: The action to execute (notify, throttle, suspend, reject)
        day_offset: Policy step day
        note: Optional note for the action
        overdue_days: Actual account overdue age after grace, when known

    Returns:
        Outcome string describing what was done
    """
    account_id = str(case.account_id)

    if action == DunningAction.notify and _dunning_shield_reason(db, case.account_id):
        return "shielded"

    # Race + shield guard for enforcing actions. The run reads every account's
    # balance once at the top and only gets here much later, so the decision is
    # stale: a payment may have landed, or an arrangement been granted, in the
    # meantime. Lock the subscriber row, then RE-READ the live balance and the
    # arrangement/proof shield before acting — so we never suspend a customer
    # who just paid or who has an approved payment plan.
    if action in _ENFORCING_ACTIONS:
        subscriber = db.execute(
            select(Subscriber).where(Subscriber.id == case.account_id).with_for_update()
        ).scalar_one_or_none()
        if subscriber is None:
            return "account_not_found"
        if not has_overdue_balance(db, account_id):
            return "balance_cleared"
        prepaid_skip = _prepaid_balance_gate_skip_reason(db, subscriber)
        if prepaid_skip:
            return prepaid_skip
        shield = _dunning_shield_reason(db, case.account_id)
        if shield:
            logger.info(
                "Dunning %s skipped for account %s: %s",
                action.value,
                account_id,
                shield,
            )
            return "shielded"
        minimum_age_skip = _minimum_enforcement_age_skip_reason(
            db, subscriber, day_offset if overdue_days is None else overdue_days
        )
        if minimum_age_skip:
            return minimum_age_skip
        from app.services.billing_enforcement_guards import (
            billing_enforcement_health,
        )

        health = billing_enforcement_health(db)
        if not health.ok:
            logger.warning(
                "Dunning %s blocked for account %s by billing enforcement "
                "health gate: reasons=%s details=%s",
                action.value,
                account_id,
                ",".join(health.reasons),
                health.details,
            )
            return "enforcement_health_blocked"

        # Phase 6 (audit-first): record whether this enforcing dunning action
        # would be deferred by the enforcement time-of-day window — WITHOUT
        # skipping yet. Flip to actually gating once the would_gate logs confirm
        # the window config. See docs/designs/BILLING_ENFORCEMENT_WINDOW.md.
        if not enforcement_window.within_enforcement_window(db):
            logger.info(
                "enforcement_window_audit",
                extra={
                    "event": "enforcement_window_audit",
                    "path": "dunning",
                    "action": action.value,
                    "account_id": account_id,
                    "would_gate": True,
                    "timezone": enforcement_window.resolve_timezone_name(db),
                },
            )

    if action == DunningAction.notify:
        _create_suspension_warning_notification(
            db, account_id, day_offset, note, invoice_id=invoice_id
        )
        return "notification_sent"

    elif action == DunningAction.suspend:
        suspended = _suspend_account(
            db,
            account_id,
            reason=EnforcementReason.overdue,
            source=f"dunning_case:{case.id}",
        )
        if suspended:
            _create_suspension_notification(db, account_id)
            return "suspended"
        return "already_suspended"

    elif action == DunningAction.throttle:
        # Apply throttle RADIUS profile to reduce bandwidth
        success, count = _throttle_account(db, account_id)
        if success and count > 0:
            _create_throttle_notification(db, account_id, day_offset)
            return "throttled"
        elif success:
            return "no_credentials_to_throttle"
        return "throttle_failed"

    elif action == DunningAction.reject:
        suspended = _suspend_account(
            db,
            account_id,
            reason=EnforcementReason.overdue,
            source=f"dunning_case:{case.id}",
        )
        if suspended:
            _create_suspension_notification(db, account_id)
            return "rejected"
        return "already_rejected"

    return "unknown_action"


class DunningCases(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DunningCaseCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.collections, "default_dunning_case_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, DunningCaseStatus, "status"
                )
        case = DunningCase(**data)
        db.add(case)
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def get(db: Session, case_id: str):
        case = db.get(DunningCase, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        return case

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DunningCase)
        if account_id:
            query = query.filter(DunningCase.account_id == account_id)
        if status:
            query = query.filter(
                DunningCase.status == validate_enum(status, DunningCaseStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DunningCase.created_at, "status": DunningCase.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, case_id: str, payload: DunningCaseUpdate):
        case = db.get(DunningCase, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(case, key, value)
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def delete(db: Session, case_id: str):
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        db.delete(case)
        db.commit()

    @staticmethod
    def pause(db: Session, case_id: str, notes: str | None = None) -> DunningCase:
        """Pause a dunning case."""
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        if case.status not in (DunningCaseStatus.open,):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot pause case with status {case.status.value}",
            )
        case.status = DunningCaseStatus.paused
        if notes:
            case.notes = (case.notes + "\n" + notes) if case.notes else notes
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            None,
            outcome="paused",
            notes=notes or "Case paused",
        )
        # Emit dunning.paused event
        emit_event(
            db,
            EventType.dunning_paused,
            {
                "case_id": str(case.id),
                "account_id": str(case.account_id),
                "reason": notes or "Case paused",
            },
            account_id=case.account_id,
        )
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def resume(db: Session, case_id: str, notes: str | None = None) -> DunningCase:
        """Resume a paused dunning case."""
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        if case.status != DunningCaseStatus.paused:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot resume case with status {case.status.value}",
            )
        case.status = DunningCaseStatus.open
        if notes:
            case.notes = (case.notes + "\n" + notes) if case.notes else notes
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            None,
            outcome="resumed",
            notes=notes or "Case resumed",
        )
        db.flush()
        _refresh_account_status(db, case.account_id)
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def close(
        db: Session,
        case_id: str,
        notes: str | None = None,
        skip_payment_check: bool = False,
    ) -> DunningCase:
        """Close a dunning case manually.

        Args:
            db: Database session
            case_id: The dunning case ID
            notes: Optional notes for the closure
            skip_payment_check: If True, skip verification that invoices are paid.
                               Use with caution - only for administrative overrides.

        Returns:
            The closed dunning case

        Raises:
            HTTPException: If case not found, already closed, or has unpaid invoices
        """
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        if case.status in (DunningCaseStatus.closed, DunningCaseStatus.resolved):
            raise HTTPException(
                status_code=400,
                detail=f"Case is already {case.status.value}",
            )

        # Verify no overdue invoices unless explicitly skipped
        if not skip_payment_check:
            overdue_invoices = (
                db.query(Invoice)
                .filter(Invoice.account_id == case.account_id)
                .filter(Invoice.balance_due > 0)
                .filter(Invoice.is_active.is_(True))
                .filter(
                    Invoice.status.in_(
                        [
                            InvoiceStatus.issued,
                            InvoiceStatus.partially_paid,
                            InvoiceStatus.overdue,
                        ]
                    )
                )
                .count()
            )
            if overdue_invoices > 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot close case: account has {overdue_invoices} unpaid invoice(s). "
                    "Pay invoices first or use skip_payment_check=True for admin override.",
                )

        now = datetime.now(UTC)
        case.status = DunningCaseStatus.closed
        case.resolved_at = now
        if notes:
            case.notes = (case.notes + "\n" + notes) if case.notes else notes
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            None,
            outcome="closed",
            notes=notes or "Case closed manually",
        )

        # Restore any throttled credentials
        _restore_throttle(db, str(case.account_id))
        db.flush()
        _refresh_account_status(db, case.account_id)

        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def add_note(db: Session, case_id: str, note: str) -> DunningCase:
        """Add a note to a dunning case."""
        case = cast(DunningCase | None, db.get(DunningCase, case_id))
        if not case:
            raise HTTPException(status_code=404, detail="Dunning case not found")
        case.notes = (case.notes + "\n" + note) if case.notes else note
        _create_action_log(
            db,
            case,
            DunningAction.notify,
            case.current_step,
            None,
            outcome="note_added",
            notes=note,
        )
        db.commit()
        db.refresh(case)
        return case

    @staticmethod
    def get_status_counts(db: Session) -> dict:
        """Get counts of dunning cases by status.

        Returns:
            Dict with keys: 'open', 'paused', 'resolved', 'closed'
            Each value is the count of cases in that status.
        """
        counts = (
            db.query(DunningCase.status, func.count(DunningCase.id))
            .group_by(DunningCase.status)
            .all()
        )
        result = {"open": 0, "paused": 0, "resolved": 0, "closed": 0}
        for status, count in counts:
            if status == DunningCaseStatus.open:
                result["open"] = count
            elif status == DunningCaseStatus.paused:
                result["paused"] = count
            elif status == DunningCaseStatus.resolved:
                result["resolved"] = count
            elif status == DunningCaseStatus.closed:
                result["closed"] = count
        return result


class DunningActionLogs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DunningActionLogCreate):
        action = DunningActionLog(**payload.model_dump())
        db.add(action)
        db.commit()
        db.refresh(action)
        return action

    @staticmethod
    def get(db: Session, action_id: str):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        return action

    @staticmethod
    def list(
        db: Session,
        case_id: str | None,
        invoice_id: str | None,
        payment_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DunningActionLog)
        if case_id:
            query = query.filter(DunningActionLog.case_id == case_id)
        if invoice_id:
            query = query.filter(DunningActionLog.invoice_id == invoice_id)
        if payment_id:
            query = query.filter(DunningActionLog.payment_id == payment_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "executed_at": DunningActionLog.executed_at,
                "action": DunningActionLog.action,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, action_id: str, payload: DunningActionLogUpdate):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(action, key, value)
        db.commit()
        db.refresh(action)
        return action

    @staticmethod
    def delete(db: Session, action_id: str):
        action = db.get(DunningActionLog, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Dunning action not found")
        db.delete(action)
        db.commit()


class DunningWorkflow(ListResponseMixin):
    @staticmethod
    def run(db: Session, payload: DunningRunRequest) -> DunningRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        invoices = (
            db.query(Invoice)
            .filter(Invoice.balance_due > 0)
            .filter(Invoice.due_at.is_not(None))
            .filter(Invoice.due_at <= run_at)
            .filter(Invoice.is_active.is_(True))
            .filter(collectible_ar_invoice_filter())
            # Only collectible invoices drive dunning. draft/void/written_off
            # rows must never create a case even if they retain a positive
            # balance_due (a stale value elsewhere would otherwise dun a debt
            # that isn't owed).
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .all()
        )
        overdue_accounts: dict[UUID, list[Invoice]] = {}
        for invoice in invoices:
            if payload.dry_run:
                prepaid_overlap_hold = (
                    invoice_paid_prepaid_overlap(db, invoice) is not None
                )
            else:
                prepaid_overlap_hold = apply_prepaid_overlap_hold(db, invoice)
            if (invoice.metadata_ or {}).get(
                "reconciliation_hold"
            ) or prepaid_overlap_hold:
                continue
            account_id = coerce_uuid(str(invoice.account_id))
            overdue_accounts.setdefault(account_id, []).append(invoice)
            if not payload.dry_run and invoice.status in {
                InvoiceStatus.issued,
                InvoiceStatus.partially_paid,
            }:
                invoice.status = InvoiceStatus.overdue
        # Dunning is a postpaid collections workflow. Prepaid service cuts are
        # owned by prepaid_balance_sweep using account available balance; legacy
        # prepaid AR rows should be cleaned/reclassified, not dunned.
        enforce_mode_filter = Subscription.billing_mode == BillingMode.postpaid
        postpaid_account_ids = {
            coerce_uuid(str(row[0]))
            for row in (
                db.query(Subscription.subscriber_id)
                .filter(enforce_mode_filter)
                .filter(
                    # ``blocked`` (recoverable non-payment) stays in scope so a
                    # walled non-payer still gets dunning cases that can recover
                    # them. See COLLECTIBLE_SERVICE_STATUSES.
                    Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES)
                )
                .distinct()
                .all()
            )
        }
        account_ids = list(overdue_accounts.keys())
        accounts = {
            coerce_uuid(str(account.id)): account
            for account in (
                db.query(Subscriber).filter(Subscriber.id.in_(account_ids)).all()
                if account_ids
                else []
            )
        }
        shield_reasons = _bulk_dunning_shield_reasons(db, set(account_ids))
        open_cases_by_account: dict[UUID, DunningCase] = {}
        if account_ids:
            open_cases = (
                db.query(DunningCase)
                .filter(DunningCase.account_id.in_(account_ids))
                .filter(
                    DunningCase.status.in_(
                        [DunningCaseStatus.open, DunningCaseStatus.paused]
                    )
                )
                .order_by(
                    DunningCase.account_id.asc(),
                    DunningCase.started_at.desc(),
                )
                .all()
            )
            for open_case in open_cases:
                open_cases_by_account.setdefault(
                    coerce_uuid(str(open_case.account_id)), open_case
                )
        steps_by_policy: dict[str, list[PolicyDunningStep]] = {}
        cases_created = 0
        actions_created = 0
        skipped = 0
        for account_id, account_invoices in overdue_accounts.items():
            account = accounts.get(account_id)
            if not account:
                skipped += 1
                continue
            if account_id not in postpaid_account_ids:
                skipped += 1
                continue
            if shield_reasons.get(account_id):
                skipped += 1
                continue

            policy_set_id = _resolve_policy_set_for_account(db, str(account_id))
            if not policy_set_id:
                skipped += 1
                continue
            policy_cache_key = str(policy_set_id)
            steps = steps_by_policy.get(policy_cache_key)
            if steps is None:
                steps = _resolve_dunning_steps(db, policy_cache_key)
                steps_by_policy[policy_cache_key] = steps
            if not steps:
                skipped += 1
                continue

            # Calculate max overdue days accounting for grace period
            max_days = max(
                _resolve_overdue_days(inv, run_at, account, db)
                for inv in account_invoices
            )

            # If all invoices are within grace period, skip dunning
            if max_days <= 0:
                skipped += 1
                continue

            case = open_cases_by_account.get(account_id)
            if not case:
                case = DunningCase(
                    account_id=account_id,
                    policy_set_id=policy_set_id,
                    status=DunningCaseStatus.open,
                    started_at=run_at,
                )
                if not payload.dry_run:
                    db.add(case)
                    db.flush()
                    _refresh_account_status(db, account_id)
                    # Emit dunning.started event
                    emit_event(
                        db,
                        EventType.dunning_started,
                        {
                            "case_id": str(case.id),
                            "account_id": str(account_id),
                            "policy_set_id": str(policy_set_id),
                            "max_days_overdue": max_days,
                        },
                        account_id=account_id,
                    )
                cases_created += 1
            else:
                if not payload.dry_run:
                    case.policy_set_id = policy_set_id
            if case.status == DunningCaseStatus.paused:
                # Paused cases are on hold by an operator — never execute
                # escalation steps until the case is resumed.
                logger.debug(
                    "Skipping dunning steps for paused case %s (account %s)",
                    case.id,
                    account_id,
                )
                skipped += 1
                continue
            oldest_invoice = min(
                account_invoices,
                key=lambda inv: inv.due_at or run_at,
            )
            step = None
            for candidate in steps:
                if candidate.day_offset <= max_days:
                    step = candidate
            if not step:
                continue
            if case.current_step is None or step.day_offset > case.current_step:
                if not payload.dry_run:
                    # Execute the dunning action (notify, suspend, throttle, reject)
                    outcome = _execute_dunning_action(
                        db,
                        case,
                        step.action,
                        step.day_offset,
                        step.note,
                        overdue_days=max_days,
                        invoice_id=str(oldest_invoice.id),
                    )
                    _create_action_log(
                        db,
                        case,
                        step.action,
                        step.day_offset,
                        str(oldest_invoice.id),
                        outcome=outcome,
                        notes=step.note,
                    )
                    if outcome not in _NON_ADVANCING_DUNNING_OUTCOMES:
                        case.current_step = step.day_offset

                    # Emit dunning.action_executed event
                    emit_event(
                        db,
                        EventType.dunning_action_executed,
                        {
                            "case_id": str(case.id),
                            "account_id": str(account_id),
                            "action": step.action.value,
                            "day_offset": step.day_offset,
                            "overdue_days": max_days,
                            "outcome": outcome,
                            "invoice_id": str(oldest_invoice.id),
                        },
                        account_id=account_id,
                    )
                actions_created += 1
        if not payload.dry_run:
            if overdue_accounts:
                open_cases = (
                    db.query(DunningCase)
                    # Only auto-resolve OPEN cases. A paused case is an operator
                    # hold ("human owns this") and must not be silently resolved
                    # by a clean run / incoming payment.
                    .filter(DunningCase.status == DunningCaseStatus.open)
                    .filter(
                        DunningCase.account_id.notin_(list(overdue_accounts.keys()))
                    )
                    .all()
                )
            else:
                open_cases = (
                    db.query(DunningCase)
                    .filter(DunningCase.status == DunningCaseStatus.open)
                    .all()
                )
            if open_cases:
                now = datetime.now(UTC)
                for case in open_cases:
                    case.status = DunningCaseStatus.resolved
                    case.resolved_at = now
                    _create_action_log(
                        db,
                        case,
                        DunningAction.notify,
                        case.current_step,
                        None,
                        outcome="resolved",
                        notes="Resolved with no overdue invoices",
                    )
                    # Emit dunning.resolved event
                    emit_event(
                        db,
                        EventType.dunning_resolved,
                        {
                            "case_id": str(case.id),
                            "account_id": str(case.account_id),
                            "reason": "no_overdue_invoices",
                        },
                        account_id=case.account_id,
                    )
                    # Restore throttled credentials if any
                    _restore_throttle(db, str(case.account_id))
                    db.flush()
                    _refresh_account_status(db, case.account_id)
        if not payload.dry_run:
            db.commit()
        return DunningRunResponse(
            run_at=run_at,
            accounts_scanned=len(overdue_accounts),
            cases_created=cases_created,
            actions_created=actions_created,
            skipped=skipped,
        )

    @staticmethod
    def resolve_cases_for_account(
        db: Session,
        account_id: str,
        invoice_id: str | None = None,
        commit: bool = True,
    ) -> int:
        cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id == account_id)
            # Only auto-resolve OPEN cases on payment; a paused case is an
            # operator hold and must be released by a human, not by a payment.
            .filter(DunningCase.status == DunningCaseStatus.open)
            .all()
        )
        if not cases:
            return 0
        now = datetime.now(UTC)
        for case in cases:
            case.status = DunningCaseStatus.resolved
            case.resolved_at = now
            _create_action_log(
                db,
                case,
                DunningAction.notify,
                case.current_step,
                invoice_id,
                outcome="resolved",
                notes="Resolved after payment",
            )
        db.flush()
        _refresh_account_status(db, account_id)
        if commit:
            db.commit()
        return len(cases)


class BillingEnforcementReconciler:
    """Single billing enforcement writer.

    Invoice generation creates AR for every production billing mode. Service
    enforcement converges here: invoice due dates + policy decide
    notify/throttle/suspend/restore through the dunning case and account
    lifecycle machinery, while prepaid enforcing actions are gated by local
    ledger available balance.
    """

    @staticmethod
    def _settle_due_credit_before_dunning(
        db: Session, run_at: datetime
    ) -> dict[str, int | str]:
        """Apply payment-backed credit to due invoices before escalation."""
        enabled = settings_spec.resolve_value(
            db,
            SettingDomain.collections,
            "billing_enforcement_settle_credit_before_dunning_enabled",
        )
        if not (
            enabled is True
            or str(enabled).strip().lower() in {"1", "true", "yes", "on"}
        ):
            return {
                "credit_accounts_scanned": 0,
                "credit_accounts_settled": 0,
                "credit_invoices_touched": 0,
                "credit_settlement_errors": 0,
                "credit_applied": "0.00",
            }

        from app.services.billing.reconcile_unposted import (
            settle_open_invoices_from_credit,
        )

        account_ids = [
            str(row[0])
            for row in (
                db.query(Invoice.account_id)
                .filter(Invoice.is_active.is_(True))
                .filter(Invoice.balance_due > 0)
                .filter(
                    Invoice.status.in_(
                        [
                            InvoiceStatus.issued,
                            InvoiceStatus.partially_paid,
                            InvoiceStatus.overdue,
                        ]
                    )
                )
                .filter(
                    or_(
                        Invoice.status == InvoiceStatus.overdue,
                        and_(
                            Invoice.due_at.is_not(None),
                            Invoice.due_at <= run_at,
                        ),
                    )
                )
                .distinct()
                .all()
            )
        ]
        stats: dict[str, int | str] = {
            "credit_accounts_scanned": len(account_ids),
            "credit_accounts_settled": 0,
            "credit_invoices_touched": 0,
            "credit_settlement_errors": 0,
            "credit_applied": "0.00",
        }
        total_applied = Decimal("0.00")
        for account_id in account_ids:
            try:
                result = settle_open_invoices_from_credit(db, account_id)
                if result.changed:
                    total_applied += result.applied
                    stats["credit_accounts_settled"] = (
                        int(stats["credit_accounts_settled"]) + 1
                    )
                    stats["credit_invoices_touched"] = int(
                        stats["credit_invoices_touched"]
                    ) + len(result.invoices_touched)
                    if not has_overdue_balance(db, account_id):
                        db.flush()
                        from app.services.account_lifecycle import (
                            compute_account_status,
                        )

                        invoice_id = (
                            result.invoices_settled[0]
                            if result.invoices_settled
                            else (
                                result.invoices_touched[0]
                                if result.invoices_touched
                                else None
                            )
                        )
                        try:
                            with db.begin_nested():
                                restore_account_services(
                                    db, account_id, invoice_id=invoice_id
                                )
                                compute_account_status(db, account_id)
                        except Exception:
                            logger.exception(
                                "billing_enforcement_credit_restore_failed",
                                extra={
                                    "event": (
                                        "billing_enforcement_credit_restore_failed"
                                    ),
                                    "account_id": account_id,
                                    "invoice_id": invoice_id,
                                },
                            )
                db.commit()
            except Exception:
                db.rollback()
                stats["credit_settlement_errors"] = (
                    int(stats["credit_settlement_errors"]) + 1
                )
                logger.exception(
                    "billing_enforcement_credit_settlement_failed",
                    extra={
                        "event": "billing_enforcement_credit_settlement_failed",
                        "account_id": account_id,
                    },
                )
        stats["credit_applied"] = str(total_applied)
        return stats

    @staticmethod
    def run(
        db: Session, payload: BillingEnforcementRunRequest
    ) -> BillingEnforcementRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        credit_stats: dict[str, int | str] = {
            "credit_accounts_scanned": 0,
            "credit_accounts_settled": 0,
            "credit_invoices_touched": 0,
            "credit_settlement_errors": 0,
            "credit_applied": "0.00",
        }
        if not payload.dry_run:
            credit_stats = (
                BillingEnforcementReconciler._settle_due_credit_before_dunning(
                    db, run_at
                )
            )
        dunning = DunningWorkflow.run(
            db,
            DunningRunRequest(run_at=run_at, dry_run=payload.dry_run),
        )
        return BillingEnforcementRunResponse(
            run_at=dunning.run_at,
            accounts_scanned=dunning.accounts_scanned,
            cases_created=dunning.cases_created,
            actions_created=dunning.actions_created,
            skipped=dunning.skipped,
            dunning_accounts_scanned=dunning.accounts_scanned,
            dunning_cases_created=dunning.cases_created,
            dunning_actions_created=dunning.actions_created,
            dunning_skipped=dunning.skipped,
            credit_accounts_scanned=int(credit_stats["credit_accounts_scanned"]),
            credit_accounts_settled=int(credit_stats["credit_accounts_settled"]),
            credit_invoices_touched=int(credit_stats["credit_invoices_touched"]),
            credit_settlement_errors=int(credit_stats["credit_settlement_errors"]),
            credit_applied=str(credit_stats["credit_applied"]),
        )


def _clear_prepaid_dunning_flags(db: Session, account_id: str) -> None:
    """Clear the prepaid low-balance / scheduled-deactivation timestamps.

    A payment or top-up that restores the account makes these stale; clearing
    them here — instead of waiting for the next collections sweep — stops a
    just-paid customer from being deactivated on a pending timer. The sweep
    re-sets them if the account is still below its minimum balance.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is not None and (
        account.prepaid_low_balance_at is not None
        or account.prepaid_deactivation_at is not None
    ):
        account.prepaid_low_balance_at = None
        account.prepaid_deactivation_at = None


def restore_account_services(
    db: Session,
    account_id: str,
    invoice_id: str | None = None,
) -> int:
    """Restore account/subscriptions and resolve dunning cases after payment."""
    restored = _restore_account(db, account_id)
    DunningWorkflow.resolve_cases_for_account(
        db,
        account_id,
        invoice_id=invoice_id,
        commit=False,
    )
    _clear_prepaid_dunning_flags(db, account_id)
    return restored


dunning_cases = DunningCases()
dunning_action_logs = DunningActionLogs()
dunning_workflow = DunningWorkflow()
billing_enforcement_reconciler = BillingEnforcementReconciler()
