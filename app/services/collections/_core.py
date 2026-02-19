import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func
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
from app.models.subscriber import Subscriber, SubscriberStatus
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
from app.services import settings_spec
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


def _parse_blocking_time(value: str | None) -> time | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


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
    """Compute prepaid available balance as credit minus open invoice balance."""
    from app.services.billing._common import get_account_credit_balance

    credit_balance = get_account_credit_balance(db, account_id)
    open_balance = (
        db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue]
            )
        )
        .scalar()
    ) or Decimal("0.00")
    return Decimal(str(credit_balance)) - Decimal(str(open_balance))


def _resolve_policy_set_for_account(db: Session, account_id: str):
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account_id)
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.suspended, SubscriptionStatus.pending]
            )
        )
        .options(
            selectinload(Subscription.offer_version),
            selectinload(Subscription.offer),
        )
        .all()
    )
    if not subscriptions:
        return None
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
    return None


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


def _suspend_account(db: Session, account_id: str) -> bool:
    """Suspend account and all active subscriptions.

    Returns True if account was suspended, False if already suspended/canceled.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account:
        logger.warning(f"Cannot suspend account {account_id}: account not found")
        return False

    if account.status == SubscriberStatus.suspended:
        logger.info(f"Account {account_id} already suspended")
        return False

    if account.status == SubscriberStatus.canceled:
        logger.info(f"Account {account_id} is canceled, skipping suspension")
        return False

    account.status = SubscriberStatus.suspended
    suspended_count = 0

    # Suspend all active (and pending) subscriptions.
    subscriptions = (
        db.query(Subscription)
        .options(selectinload(Subscription.offer))
        .filter(Subscription.subscriber_id == account.id)
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.pending]
            )
        )
        .all()
    )
    for sub in subscriptions:
        from_status = sub.status.value if sub.status else None
        sub.status = SubscriptionStatus.suspended
        suspended_count += 1
        # Emit subscription.suspended event
        emit_event(
            db,
            EventType.subscription_suspended,
            {
                "subscription_id": str(sub.id),
                "offer_name": sub.offer.name if sub.offer else None,
                "from_status": from_status,
                "to_status": "suspended",
                "reason": "dunning",
            },
            subscription_id=sub.id,
            account_id=sub.subscriber_id,
        )

    # Emit subscriber.suspended event
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

    logger.info(f"Suspended account {account_id} with {suspended_count} subscriptions")
    return True


def _restore_account(db: Session, account_id: str) -> int:
    """Restore account and suspended subscriptions after payment."""
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account:
        logger.warning(f"Cannot restore account {account_id}: account not found")
        return 0
    if account.status == SubscriberStatus.canceled:
        logger.info(f"Account {account_id} is canceled, skipping restore")
        return 0
    was_suspended = account.status in {SubscriberStatus.suspended, SubscriberStatus.delinquent}
    if was_suspended:
        account.status = SubscriberStatus.active
    restored_count = 0
    now = datetime.now(UTC)
    subscriptions = (
        db.query(Subscription)
        .options(selectinload(Subscription.offer))
        .filter(Subscription.subscriber_id == account.id)
        .filter(Subscription.status == SubscriptionStatus.suspended)
        .all()
    )
    for sub in subscriptions:
        if sub.end_at and sub.end_at <= now:
            continue
        sub.status = SubscriptionStatus.active
        restored_count += 1
        # Emit subscription.resumed event
        emit_event(
            db,
            EventType.subscription_resumed,
            {
                "subscription_id": str(sub.id),
                "offer_name": sub.offer.name if sub.offer else None,
                "from_status": "suspended",
                "to_status": "active",
                "reason": "payment_received",
            },
            subscription_id=sub.id,
            account_id=sub.subscriber_id,
        )

    if restored_count and was_suspended:
        # Emit subscriber.reactivated event
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
        logger.info(f"Restored {restored_count} subscriptions for account {account_id}")
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
        if cred.radius_profile_id and str(cred.radius_profile_id) != str(throttle_profile_id):
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
            .filter(Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.suspended]))
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
        logger.info(f"Restored {restored_count} throttled credentials for account {account_id}")

    return restored_count


def _create_throttle_notification(db: Session, account_id: str, days_overdue: int) -> None:
    """Create email notification that account has been throttled."""
    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )

    email = _get_account_email(db, account_id)
    if not email:
        logger.warning(f"Cannot create throttle notification for account {account_id}: no email found")
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
        logger.warning(f"Cannot create suspension warning notification for account {account_id}: no email found")
        return

    body = note or f"Your account is {days_overdue} days past due. Please make a payment to avoid service suspension."
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
        logger.warning(f"Cannot create suspension notification for account {account_id}: no email found")
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
        settings_spec.resolve_value(db, SettingDomain.collections, "prepaid_warning_subject")
        or "Low Balance Warning"
    )
    body_template = str(
        settings_spec.resolve_value(db, SettingDomain.collections, "prepaid_warning_body")
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

    subject = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_deactivation_subject"
    ) or "Service Deactivated"

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
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.suspended, SubscriptionStatus.pending]
            )
        )
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .all()
    )
    canceled_count = 0
    for sub in subscriptions:
        if sub.status == SubscriptionStatus.canceled:
            continue
        previous_status = sub.status
        sub.status = SubscriptionStatus.canceled
        sub.canceled_at = run_at
        sub.cancel_reason = "prepaid_deactivation"
        emit_event(
            db,
            EventType.subscription_canceled,
            {
                "subscription_id": str(sub.id),
                "offer_name": sub.offer.name if sub.offer else None,
                "from_status": previous_status.value if previous_status else None,
                "to_status": "canceled",
                "reason": "prepaid_deactivation",
            },
            subscription_id=sub.id,
            account_id=sub.subscriber_id,
        )
        canceled_count += 1

    account = db.get(Subscriber, coerce_uuid(account_id))
    if account and account.status != SubscriberStatus.canceled:
        account.status = SubscriberStatus.canceled

    _create_prepaid_deactivation_notification(db, account_id)
    return canceled_count


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

    if action == DunningAction.notify:
        _create_suspension_warning_notification(db, account_id, day_offset, note)
        return "notification_sent"

    elif action == DunningAction.suspend:
        suspended = _suspend_account(db, account_id)
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
        # Reject action - similar to suspend but more severe
        suspended = _suspend_account(db, account_id)
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
                        [InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue]
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
            {"executed_at": DunningActionLog.executed_at, "action": DunningActionLog.action},
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
            if (
                not payload.dry_run
                and invoice.status in {InvoiceStatus.issued, InvoiceStatus.partially_paid}
            ):
                invoice.status = InvoiceStatus.overdue
        postpaid_account_ids = {
            row[0]
            for row in (
                db.query(Subscription.subscriber_id)
                .filter(Subscription.billing_mode == BillingMode.postpaid)
                .filter(
                    Subscription.status.in_(
                        [SubscriptionStatus.active, SubscriptionStatus.suspended, SubscriptionStatus.pending]
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
                _resolve_overdue_days(inv, run_at, account)
                for inv in account_invoices
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
                    .filter(
                        DunningCase.status.in_(
                            [DunningCaseStatus.open, DunningCaseStatus.paused]
                        )
                    )
                    .filter(DunningCase.account_id.notin_(list(overdue_accounts.keys())))
                    .all()
                )
            else:
                open_cases = (
                    db.query(DunningCase)
                    .filter(
                        DunningCase.status.in_(
                            [DunningCaseStatus.open, DunningCaseStatus.paused]
                        )
                    )
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
            .filter(
                DunningCase.status.in_([DunningCaseStatus.open, DunningCaseStatus.paused])
            )
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
    def run(db: Session, payload: PrepaidEnforcementRunRequest) -> PrepaidEnforcementRunResponse:
        run_at = payload.run_at or datetime.now(UTC)
        timezone_name = str(
            settings_spec.resolve_value(db, SettingDomain.scheduler, "timezone") or "UTC"
        )
        blocking_time_value = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_blocking_time"
        )
        blocking_time = _parse_blocking_time(
            str(blocking_time_value) if blocking_time_value is not None else None
        )
        skip_weekends = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_skip_weekends"
        )
        skip_holidays = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_skip_holidays"
        ) or []
        grace_days_default_raw = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_grace_days"
        )
        deactivation_days_default_raw = settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_deactivation_days"
        )
        try:
            grace_days_default = (
                int(str(grace_days_default_raw)) if grace_days_default_raw is not None else 0
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

        try:
            local_run_at = run_at.astimezone(ZoneInfo(timezone_name))
        except Exception:
            local_run_at = run_at
        run_date = local_run_at.date()

        if blocking_time and local_run_at.time() < blocking_time:
            return PrepaidEnforcementRunResponse(
                run_at=run_at,
                accounts_scanned=0,
                accounts_warned=0,
                accounts_suspended=0,
                accounts_deactivated=0,
                skipped=0,
            )
        if skip_weekends and local_run_at.weekday() >= 5:
            return PrepaidEnforcementRunResponse(
                run_at=run_at,
                accounts_scanned=0,
                accounts_warned=0,
                accounts_suspended=0,
                accounts_deactivated=0,
                skipped=0,
            )
        if isinstance(skip_holidays, list) and run_date.isoformat() in skip_holidays:
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
                    [SubscriptionStatus.active, SubscriptionStatus.suspended, SubscriptionStatus.pending]
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
                        [SubscriptionStatus.active, SubscriptionStatus.suspended, SubscriptionStatus.pending]
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

        for (account_id,) in prepaid_accounts:
            accounts_scanned += 1
            if account_id in postpaid_account_ids:
                skipped += 1
                continue
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
                else (default_threshold if default_threshold is not None else "0.00")
            )
            threshold = Decimal(str(threshold_value))
            balance = _resolve_prepaid_available_balance(db, str(account_id))
            if balance >= threshold:
                if not payload.dry_run:
                    if account.prepaid_low_balance_at or account.prepaid_deactivation_at:
                        account.prepaid_low_balance_at = None
                        account.prepaid_deactivation_at = None
                continue

            low_balance_at = account.prepaid_low_balance_at or run_at
            if not payload.dry_run and account.prepaid_low_balance_at is None:
                account.prepaid_low_balance_at = run_at
                if deactivation_days_default:
                    account.prepaid_deactivation_at = run_at + timedelta(days=deactivation_days_default)
            grace_days = (
                int(account.grace_period_days)
                if account.grace_period_days is not None
                else grace_days_default
            )
            grace_until = low_balance_at + timedelta(days=grace_days) if grace_days > 0 else low_balance_at
            if run_at < grace_until:
                if not payload.dry_run:
                    _create_prepaid_warning_notification(
                        db, str(account_id), str(balance), str(threshold)
                    )
                accounts_warned += 1
                continue

            deactivation_at = account.prepaid_deactivation_at
            if deactivation_at and run_at >= deactivation_at:
                if not payload.dry_run:
                    _deactivate_prepaid_subscriptions(db, str(account_id), run_at)
                accounts_deactivated += 1
                continue

            if not payload.dry_run:
                _suspend_account(db, str(account_id))
            accounts_suspended += 1

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
            .filter(
                DunningCase.status.in_(
                    [DunningCaseStatus.open, DunningCaseStatus.paused]
                )
            )
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
    return restored


dunning_cases = DunningCases()
dunning_action_logs = DunningActionLogs()
dunning_workflow = DunningWorkflow()
prepaid_enforcement = PrepaidEnforcement()
