import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    DunningAction,
    PolicyDunningStep,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningActionLog, DunningCase, DunningCaseStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningActionLogUpdate,
    DunningCaseCreate,
    DunningCaseUpdate,
    DunningRunRequest,
    DunningRunResponse,
    PrepaidEnforcementRunRequest,
    PrepaidEnforcementRunResponse,
)
from app.services import enforcement_window, settings_spec
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


def _get_prepaid_last_run_date(db: Session) -> date | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.collections)
        .filter(DomainSetting.key == "prepaid_last_run_date")
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting or not setting.value_text:
        return None
    try:
        return date.fromisoformat(setting.value_text)
    except ValueError:
        return None


def _set_prepaid_last_run_date(db: Session, run_date: date) -> None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.collections)
        .filter(DomainSetting.key == "prepaid_last_run_date")
        .first()
    )
    if not setting:
        setting = DomainSetting(
            domain=SettingDomain.collections,
            key="prepaid_last_run_date",
            value_text=run_date.isoformat(),
            value_json=None,
            is_active=True,
        )
        db.add(setting)
    else:
        setting.value_text = run_date.isoformat()
        setting.value_json = None
        setting.is_active = True


def _resolve_prepaid_available_balance(db: Session, account_id: str) -> Decimal:
    """Authoritative prepaid available balance.

    For Splynx-linked accounts the synced ``deposit`` IS the balance: Splynx's
    billing engine already nets invoices, payments and transactions into it,
    and it does not reconcile to any naive local recomputation (verified —
    e.g. cust 25313 deposit 31,965.11 vs payments-minus-invoices 163,236).
    Re-deriving it locally is what produced the phantom-invoice divergence, so
    we trust the net rather than recompute it.

    Native (non-Splynx) accounts have no authoritative deposit, so they fall
    back to the local ledger model: credit minus open invoice balance.

    At cutover the local ledger takes over: once a Splynx-linked account has its
    one-time opening-balance seed (see the prepaid drawdown engine), we switch
    that account to the ledger so drawdown debits and top-up credits take
    effect. The seed is the per-account switch — no risky global flip.
    """
    from app.models.billing import LedgerEntry
    from app.services.billing._common import get_account_credit_balance
    from app.services.prepaid_billing import PREPAID_OPENING_BALANCE_MEMO

    account = db.get(Subscriber, coerce_uuid(account_id))
    if (
        account is not None
        and account.splynx_customer_id is not None
        and account.deposit is not None
    ):
        # The seed (credit for positive deposits, debit for arrears) marks the
        # account as switched to the ledger; match on memo regardless of type.
        seeded = (
            db.query(LedgerEntry.id)
            .filter(LedgerEntry.account_id == coerce_uuid(account_id))
            .filter(LedgerEntry.memo == PREPAID_OPENING_BALANCE_MEMO)
            .filter(LedgerEntry.is_active.is_(True))
            .first()
        )
        if seeded is None:
            return Decimal(str(account.deposit))

    credit_balance = get_account_credit_balance(db, account_id)
    open_balance = (
        db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
        .filter(Invoice.account_id == coerce_uuid(account_id))
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
        .scalar()
    ) or Decimal("0.00")
    return Decimal(str(credit_balance)) - Decimal(str(open_balance))


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
    row = (
        db.query(Invoice.id)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.balance_due > 0)
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
        .first()
    )
    return row is not None


def _general_default_policy_set_id(db: Session, account: Subscriber):
    """The general (fallback) dunning policy for an account's billing mode.

    Configured via the collections settings ``default_prepaid_policy_set_id`` /
    ``default_postpaid_policy_set_id`` (seeded to the immediate-suspend prepaid
    policy and the 30-day postpaid policy respectively).
    """
    key = (
        "default_prepaid_policy_set_id"
        if account.billing_mode == BillingMode.prepaid
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
        .filter(
            Subscription.status.in_(
                [
                    SubscriptionStatus.active,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.pending,
                ]
            )
        )
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

    # Subtract grace period if account has one configured
    grace_period = 0
    if account and account.grace_period_days is not None:
        grace_period = int(account.grace_period_days)

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


def _suspend_account(
    db: Session,
    account_id: str,
    reason: EnforcementReason = EnforcementReason.overdue,
    source: str = "dunning",
    only_billing_mode: BillingMode | None = None,
) -> bool:
    """Suspend account via enforcement locks on its active subscriptions.

    Delegates to ``account_lifecycle.suspend_subscription`` per subscription
    and lets ``compute_account_status`` derive the subscriber status.

    ``only_billing_mode`` restricts which subscriptions are suspended. Prepaid
    *balance* enforcement passes ``BillingMode.prepaid`` so it never cuts a
    postpaid service on the same account (postpaid lapses only via dunning) —
    mirrors the scoping already in ``_deactivate_prepaid_subscriptions``.
    Dunning leaves it ``None`` to suspend the whole account on arrears.

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

    query = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account.id)
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.pending]
            )
        )
    )
    if only_billing_mode is not None:
        query = query.filter(Subscription.billing_mode == only_billing_mode)
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
        .filter(Subscription.status == SubscriptionStatus.suspended)
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


# Enforcement reasons that have been retired from the product. Locks created by
# a retired reason are obsolete and must be resolved so service is restored —
# the subsystem that would have lifted them no longer runs. Deposit-based prepaid
# enforcement was retired post-cutover (due-date dunning is the sole enforcer).
RETIRED_ENFORCEMENT_REASONS: tuple[EnforcementReason, ...] = (
    EnforcementReason.prepaid,
)


def reconcile_retired_enforcement_locks(
    db: Session,
    *,
    reasons: tuple[EnforcementReason, ...] = RETIRED_ENFORCEMENT_REASONS,
    resolved_by: str = "retired-enforcement-reconcile",
) -> dict[str, int]:
    """Resolve enforcement locks whose reason has been retired, restoring service.

    For each active lock with a retired reason, resolve it via the normal
    restore path (``restore_subscription``) so RADIUS is re-provisioned and a
    resume event is emitted. A subscription left with no other active lock is
    reactivated; one still held by another reason (e.g. ``overdue``) stays
    suspended. Per-subscription commit, idempotent, safe to re-run — once no
    retired locks remain it is a no-op.
    """
    from app.services.account_lifecycle import restore_subscription

    summary = {
        "resolved": 0,
        "restored": 0,
        "still_locked": 0,
        "stale_cleared": 0,
        "errors": 0,
    }
    for reason in reasons:
        # Pass 1: suspended subs — restore via the normal path (resolves the lock,
        # flips status to active, emits a resume event so RADIUS re-provisions).
        suspended_sub_ids = [
            row[0]
            for row in db.query(EnforcementLock.subscription_id)
            .join(Subscription, Subscription.id == EnforcementLock.subscription_id)
            .filter(EnforcementLock.is_active.is_(True))
            .filter(EnforcementLock.reason == reason)
            .filter(Subscription.status == SubscriptionStatus.suspended)
            .distinct()
            .all()
        ]
        for sub_id in suspended_sub_ids:
            try:
                restored = restore_subscription(
                    db,
                    str(sub_id),
                    trigger="admin",
                    resolved_by=resolved_by,
                    reason=reason,
                    notes=(
                        f"Enforcement reason {reason.value!r} retired; "
                        "obsolete lock resolved by reconcile."
                    ),
                    emit=True,
                )
                db.commit()
                summary["resolved"] += 1
                summary["restored" if restored else "still_locked"] += 1
            except Exception as exc:  # noqa: BLE001 - keep going, count failures
                db.rollback()
                summary["errors"] += 1
                logger.error(
                    "retired-lock reconcile failed for subscription %s (reason=%s): %s",
                    sub_id,
                    reason.value,
                    exc,
                )

        # Pass 2: locks that survive on non-suspended subs (restore_subscription
        # only resolves locks on suspended subs). The reason is retired, so these
        # are obsolete records — resolve them directly; no status/RADIUS change.
        now = datetime.now(UTC)
        stale = (
            db.query(EnforcementLock)
            .filter(EnforcementLock.is_active.is_(True))
            .filter(EnforcementLock.reason == reason)
            .all()
        )
        for lock in stale:
            lock.is_active = False
            lock.resolved_at = now
            lock.resolved_by = resolved_by
            lock.notes = (lock.notes or "") + (
                f" [reason {reason.value!r} retired; stale lock cleared]"
            )
            summary["resolved"] += 1
            summary["stale_cleared"] += 1
        if stale:
            db.commit()
    logger.info("retired-lock reconcile summary: %s", summary)
    return summary


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
        recipient=email,
        subject="Service Speed Reduced - Payment Overdue",
        body=f"Your internet speed has been reduced due to payment being {days_overdue} days overdue. "
        "Please make a payment to restore full speed.",
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created throttle notification for account {account_id}")


def _create_suspension_warning_notification(
    db: Session, account_id: str, days_overdue: int, note: str | None = None
) -> None:
    """Create email notification warning of pending suspension."""
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create suspension warning notification for account {account_id}: no email found"
        )
        return

    body = (
        note
        or f"Your account is {days_overdue} days past due. Please make a payment to avoid service suspension."
    )
    notification = Notification(
        channel=NotificationChannel.email,
        recipient=email,
        subject="Suspension Warning - Payment Overdue",
        body=body,
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created suspension warning notification for account {account_id}")


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

    # Idempotency check: don't create duplicate suspension notification within 24 hours
    recent_threshold = datetime.now(UTC) - timedelta(hours=24)
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
        recipient=email,
        subject="Account Suspended",
        body="Your account has been suspended due to non-payment. Please make a payment to restore service.",
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created suspension notification for account {account_id}")


def _create_prepaid_warning_notification(
    db: Session, account_id: str, balance: str, threshold: str
) -> None:
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    # Idempotency check: don't create duplicate warning within 24 hours
    recent_threshold = datetime.now(UTC) - timedelta(hours=24)
    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create prepaid warning notification for account {account_id}: no email found"
        )
        return

    existing_warning = (
        db.query(Notification)
        .filter(Notification.recipient == email)
        .filter(Notification.subject.like("%Low Balance%"))
        .filter(Notification.created_at > recent_threshold)
        .filter(Notification.is_active.is_(True))
        .first()
    )
    if existing_warning:
        logger.debug(
            f"Skipping prepaid warning for account {account_id}: recent warning exists"
        )
        return

    subject = str(
        settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_warning_subject"
        )
        or "Low Balance Warning"
    )
    body_template = str(
        settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_warning_body"
        )
        or (
            "Your prepaid balance is below the minimum threshold ({threshold}). "
            "Current balance: {balance}. Please top up to avoid suspension."
        )
    )
    try:
        body = body_template.format(balance=balance, threshold=threshold)
    except (KeyError, ValueError):
        body = body_template

    notification = Notification(
        channel=NotificationChannel.email,
        recipient=email,
        subject=subject,
        body=body,
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created prepaid low balance warning for account {account_id}")


def _create_prepaid_deactivation_notification(db: Session, account_id: str) -> None:
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(
            f"Cannot create prepaid deactivation notification for account {account_id}: no email found"
        )
        return

    subject = (
        settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_deactivation_subject"
        )
        or "Service Deactivated"
    )

    # Idempotency check: don't create duplicate deactivation notification within 24 hours
    recent_threshold = datetime.now(UTC) - timedelta(hours=24)
    existing = (
        db.query(Notification)
        .filter(Notification.recipient == email)
        .filter(Notification.subject == subject)
        .filter(Notification.created_at > recent_threshold)
        .filter(Notification.is_active.is_(True))
        .first()
    )
    if existing:
        logger.debug(
            f"Skipping deactivation notification for account {account_id}: recent notification exists"
        )
        return

    body = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_deactivation_body"
    ) or (
        "Your prepaid balance has been exhausted and service has been deactivated. "
        "Please contact support to restore service."
    )

    notification = Notification(
        channel=NotificationChannel.email,
        recipient=email,
        subject=subject,
        body=body,
        status=NotificationStatus.queued,
    )
    db.add(notification)
    logger.info(f"Created prepaid deactivation notification for account {account_id}")


def _deactivate_prepaid_subscriptions(
    db: Session, account_id: str, run_at: datetime
) -> int:
    """Cancel all prepaid subscriptions for an account via lifecycle operations."""
    from app.services.account_lifecycle import (
        cancel_subscription,
        compute_account_status,
    )

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .filter(
            Subscription.status.in_(
                [
                    SubscriptionStatus.active,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.pending,
                ]
            )
        )
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .all()
    )
    canceled_count = 0
    for sub in subscriptions:
        try:
            cancel_subscription(
                db,
                str(sub.id),
                cancel_reason="prepaid_deactivation",
                source="prepaid_enforcement",
            )
            canceled_count += 1
        except ValueError as e:
            if "already canceled" in str(e):
                logger.info("Skipped canceling subscription %s: %s", sub.id, e)
            else:
                logger.error("Failed to cancel subscription %s: %s", sub.id, e)
                raise

    # Always recompute account status — handles edge case where all
    # subscriptions were already canceled by a prior run
    try:
        compute_account_status(db, account_id)
    except ValueError:
        logger.warning("Could not compute account status for %s", account_id)

    _create_prepaid_deactivation_notification(db, account_id)
    return canceled_count


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
    return None


# Dunning actions that enforce against the account (and so must re-check the
# live balance + shield right before acting, not trust the run's snapshot).
_ENFORCING_ACTIONS = frozenset(
    {DunningAction.suspend, DunningAction.reject, DunningAction.throttle}
)


def _execute_dunning_action(
    db: Session,
    case: DunningCase,
    action: DunningAction,
    day_offset: int,
    note: str | None,
) -> str:
    """Execute a dunning action and return the outcome.

    Args:
        db: Database session
        case: The dunning case
        action: The action to execute (notify, throttle, suspend, reject)
        day_offset: Days overdue
        note: Optional note for the action

    Returns:
        Outcome string describing what was done
    """
    account_id = str(case.account_id)

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
        shield = _dunning_shield_reason(db, case.account_id)
        if shield:
            logger.info(
                "Dunning %s skipped for account %s: %s",
                action.value,
                account_id,
                shield,
            )
            return "shielded"

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
        _create_suspension_warning_notification(db, account_id, day_offset, note)
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
            .all()
        )
        overdue_accounts: dict[UUID, list[Invoice]] = {}
        for invoice in invoices:
            overdue_accounts.setdefault(invoice.account_id, []).append(invoice)
            if not payload.dry_run and invoice.status in {
                InvoiceStatus.issued,
                InvoiceStatus.partially_paid,
            }:
                invoice.status = InvoiceStatus.overdue
        # Dunning enforces postpaid accounts, plus prepaid_monthly (prepaid on a
        # MONTHLY-cycle offer, invoiced due-on-issue) once the cutover flag is on.
        # Default OFF keeps dunning postpaid-only — no behaviour change. Genuine
        # daily/balance prepaid has no invoices so is never in scope here.
        _pm_flag = settings_spec.resolve_value(
            db, SettingDomain.billing, "prepaid_monthly_invoicing_enabled"
        )
        include_prepaid_monthly = str(_pm_flag).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        enforce_mode_filter = Subscription.billing_mode == BillingMode.postpaid
        if include_prepaid_monthly:
            monthly_offer_ids = select(CatalogOffer.id).where(
                CatalogOffer.billing_cycle == BillingCycle.monthly
            )
            enforce_mode_filter = or_(
                Subscription.billing_mode == BillingMode.postpaid,
                Subscription.offer_id.in_(monthly_offer_ids),
            )
        postpaid_account_ids = {
            row[0]
            for row in (
                db.query(Subscription.subscriber_id)
                .filter(enforce_mode_filter)
                .filter(
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.active,
                            SubscriptionStatus.suspended,
                            SubscriptionStatus.pending,
                        ]
                    )
                )
                .distinct()
                .all()
            )
        }
        cases_created = 0
        actions_created = 0
        skipped = 0
        for account_id, account_invoices in overdue_accounts.items():
            # Fetch account to get grace_period
            account = db.get(Subscriber, account_id)
            if not account:
                skipped += 1
                continue
            if account_id not in postpaid_account_ids:
                skipped += 1
                continue

            policy_set_id = _resolve_policy_set_for_account(db, str(account_id))
            if not policy_set_id:
                skipped += 1
                continue
            steps = _resolve_dunning_steps(db, str(policy_set_id))
            if not steps:
                skipped += 1
                continue

            # Calculate max overdue days accounting for grace period
            max_days = max(
                _resolve_overdue_days(inv, run_at, account) for inv in account_invoices
            )

            # If all invoices are within grace period, skip dunning
            if max_days <= 0:
                skipped += 1
                continue

            case = (
                db.query(DunningCase)
                .filter(DunningCase.account_id == account_id)
                .filter(
                    DunningCase.status.in_(
                        [DunningCaseStatus.open, DunningCaseStatus.paused]
                    )
                )
                .order_by(DunningCase.started_at.desc())
                .first()
            )
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
                        db, case, step.action, step.day_offset, step.note
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
        if commit:
            db.commit()
        return len(cases)


class PrepaidEnforcement(ListResponseMixin):
    @staticmethod
    def run(
        db: Session, payload: PrepaidEnforcementRunRequest
    ) -> PrepaidEnforcementRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        blocking_time_value = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_blocking_time"
        )
        blocking_time = enforcement_window.parse_time(
            str(blocking_time_value) if blocking_time_value is not None else None
        )
        skip_weekends = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_skip_weekends"
        )
        skip_holidays = (
            settings_spec.resolve_value(
                db, SettingDomain.collections, "prepaid_skip_holidays"
            )
            or []
        )
        grace_days_default_raw = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_grace_days"
        )
        deactivation_days_default_raw = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_deactivation_days"
        )
        try:
            grace_days_default = (
                int(str(grace_days_default_raw))
                if grace_days_default_raw is not None
                else 0
            )
        except (TypeError, ValueError):
            grace_days_default = 0
        try:
            deactivation_days_default = (
                int(str(deactivation_days_default_raw))
                if deactivation_days_default_raw is not None
                else 0
            )
        except (TypeError, ValueError):
            deactivation_days_default = 0

        local_run_at = enforcement_window.to_local(db, run_at)
        run_date = local_run_at.date()

        if enforcement_window.window_block_reason(
            local_run_at,
            start_time=blocking_time,
            skip_weekends=bool(skip_weekends),
            skip_holidays=skip_holidays if isinstance(skip_holidays, list) else None,
        ):
            return PrepaidEnforcementRunResponse(
                run_at=run_at,
                accounts_scanned=0,
                accounts_warned=0,
                accounts_suspended=0,
                accounts_deactivated=0,
                skipped=0,
            )
        last_run_date = _get_prepaid_last_run_date(db)
        if last_run_date == run_date:
            return PrepaidEnforcementRunResponse(
                run_at=run_at,
                accounts_scanned=0,
                accounts_warned=0,
                accounts_suspended=0,
                accounts_deactivated=0,
                skipped=0,
            )

        prepaid_accounts = (
            db.query(Subscription.subscriber_id)
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
            .distinct()
            .all()
        )
        postpaid_account_ids = {
            row[0]
            for row in (
                db.query(Subscription.subscriber_id)
                .filter(Subscription.billing_mode == BillingMode.postpaid)
                .filter(
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.active,
                            SubscriptionStatus.suspended,
                            SubscriptionStatus.pending,
                        ]
                    )
                )
                .distinct()
                .all()
            )
        }

        accounts_scanned = 0
        accounts_warned = 0
        accounts_suspended = 0
        accounts_deactivated = 0
        skipped = 0

        if not payload.dry_run and db.get_bind().dialect.name == "postgresql":
            # Each account is enforced and committed independently (below), so a
            # transient row-lock (e.g. a poller holding a subscription row) only
            # skips that one account instead of aborting the whole run. A short
            # lock_timeout makes a contended FOR UPDATE fail fast into the
            # per-account skip path rather than block the run. Postgres-only;
            # SQLite (tests) has no statement-level lock timeout.
            db.execute(text("SET lock_timeout = '5s'"))

        for (account_id,) in prepaid_accounts:
            accounts_scanned += 1
            if account_id in postpaid_account_ids:
                skipped += 1
                continue
            # Per-account transaction: on any error (lock timeout, etc.) roll
            # back just this account and continue; the hourly cadence retries it.
            try:
                account = db.get(Subscriber, account_id)
                if not account:
                    skipped += 1
                    continue
                default_threshold = settings_spec.resolve_value(
                    db, SettingDomain.collections, "prepaid_default_min_balance"
                )
                threshold_value = (
                    account.min_balance
                    if account.min_balance is not None
                    else (
                        default_threshold if default_threshold is not None else "0.00"
                    )
                )
                threshold = Decimal(str(threshold_value))
                balance = _resolve_prepaid_available_balance(db, str(account_id))
                if balance >= threshold:
                    if not payload.dry_run:
                        if (
                            account.prepaid_low_balance_at
                            or account.prepaid_deactivation_at
                        ):
                            account.prepaid_low_balance_at = None
                            account.prepaid_deactivation_at = None
                            db.commit()
                    continue

                low_balance_at = account.prepaid_low_balance_at or run_at
                if not payload.dry_run and account.prepaid_low_balance_at is None:
                    account.prepaid_low_balance_at = run_at
                    if deactivation_days_default:
                        account.prepaid_deactivation_at = run_at + timedelta(
                            days=deactivation_days_default
                        )
                grace_days = (
                    int(account.grace_period_days)
                    if account.grace_period_days is not None
                    else grace_days_default
                )
                grace_until = (
                    low_balance_at + timedelta(days=grace_days)
                    if grace_days > 0
                    else low_balance_at
                )
                if run_at < grace_until:
                    if not payload.dry_run:
                        _create_prepaid_warning_notification(
                            db, str(account_id), str(balance), str(threshold)
                        )
                        db.commit()
                    accounts_warned += 1
                    continue

                deactivation_at = account.prepaid_deactivation_at
                if deactivation_at and run_at >= deactivation_at:
                    if not payload.dry_run:
                        _deactivate_prepaid_subscriptions(db, str(account_id), run_at)
                        db.commit()
                    accounts_deactivated += 1
                    continue

                if not payload.dry_run:
                    _suspend_account(
                        db,
                        str(account_id),
                        reason=EnforcementReason.prepaid,
                        source="prepaid_enforcement",
                        # Prepaid balance enforcement must only cut prepaid
                        # services; a postpaid service on the same account
                        # lapses via dunning.
                        only_billing_mode=BillingMode.prepaid,
                    )
                    db.commit()
                accounts_suspended += 1
            except Exception:
                db.rollback()
                skipped += 1
                logger.warning(
                    "prepaid enforcement: skipped account %s after error",
                    account_id,
                    exc_info=True,
                )
                continue

        if not payload.dry_run:
            _set_prepaid_last_run_date(db, run_date)
            db.commit()

        return PrepaidEnforcementRunResponse(
            run_at=run_at,
            accounts_scanned=accounts_scanned,
            accounts_warned=accounts_warned,
            accounts_suspended=accounts_suspended,
            accounts_deactivated=accounts_deactivated,
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
        if commit:
            db.commit()
        return len(cases)


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
prepaid_enforcement = PrepaidEnforcement()
