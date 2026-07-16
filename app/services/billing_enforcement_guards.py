"""Production safety guards for billing enforcement.

These checks are deliberately narrow: they do not decide whether an invoice is
collectible. They decide whether it is safe to execute a service-affecting
collections action right now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
    PaymentWebhookDeadLetter,
    PaymentWebhookDeadLetterStatus,
    TopupIntent,
)
from app.models.domain_settings import SettingDomain
from app.models.notification import Notification, NotificationStatus
from app.models.scheduler import ScheduledTask
from app.services import settings_spec

logger = logging.getLogger(__name__)


CRITICAL_NOTIFICATION_EVENT_TYPES = frozenset(
    {
        "invoice_created",
        "invoice_sent",
        "invoice_overdue",
        "suspension_warning",
        "account_suspended",
        "account_throttled",
        "service_suspended",
        "service_restored",
        "payment_received",
        "payment_failed",
    }
)

ONLINE_PAYMENT_PROVIDER_TYPES = (
    PaymentProviderType.paystack,
    PaymentProviderType.flutterwave,
)


@dataclass(frozen=True)
class EnforcementHealth:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, int | bool] = field(default_factory=dict)


def _setting_bool(
    db: Session, domain: SettingDomain, key: str, default: bool = False
) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    value = settings_spec.resolve_value(db, domain, key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _critical_notification_filter():
    return or_(
        Notification.category == "billing",
        Notification.event_type.in_(list(CRITICAL_NOTIFICATION_EVENT_TYPES)),
        Notification.subject.in_(
            [
                "Suspension Warning - Payment Overdue",
                "Account Suspended",
                "Service Speed Reduced - Payment Overdue",
            ]
        ),
    )


def notification_delivery_health(db: Session) -> EnforcementHealth:
    """Return whether critical billing notifications are drainable."""
    from app.services import control_registry

    if not control_registry.is_enabled(db, "notifications.queue"):
        return EnforcementHealth(
            ok=False,
            reasons=["notification_queue_disabled"],
            details={"notification_queue_enabled": False},
        )

    task_enabled = (
        db.query(ScheduledTask.enabled)
        .filter(
            ScheduledTask.task_name
            == "app.tasks.notifications.deliver_notification_queue"
        )
        .order_by(ScheduledTask.updated_at.desc())
        .limit(1)
        .scalar()
    )
    if task_enabled is False:
        return EnforcementHealth(
            ok=False,
            reasons=["notification_queue_task_disabled"],
            details={"notification_queue_task_enabled": False},
        )

    now = datetime.now(UTC)
    max_oldest_minutes = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_notification_max_oldest_queued_minutes",
        120,
    )
    max_failed = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_notification_max_failed",
        100,
    )
    max_stuck_sending = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_notification_max_stuck_sending",
        25,
    )
    failed_window_hours = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_notification_failed_window_hours",
        24,
    )
    sending_cutoff = now - timedelta(minutes=15)
    queued_cutoff = now - timedelta(minutes=max_oldest_minutes)
    failed_cutoff = now - timedelta(hours=failed_window_hours)

    critical = _critical_notification_filter()
    old_queued = (
        db.query(func.count(Notification.id))
        .filter(Notification.is_active.is_(True))
        .filter(critical)
        .filter(Notification.status == NotificationStatus.queued)
        .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
        .filter(Notification.created_at < queued_cutoff)
        .scalar()
        or 0
    )
    stuck_sending = (
        db.query(func.count(Notification.id))
        .filter(Notification.is_active.is_(True))
        .filter(critical)
        .filter(Notification.status == NotificationStatus.sending)
        .filter(Notification.updated_at < sending_cutoff)
        .scalar()
        or 0
    )
    recent_failed = (
        db.query(func.count(Notification.id))
        .filter(Notification.is_active.is_(True))
        .filter(critical)
        .filter(Notification.status == NotificationStatus.failed)
        .filter(Notification.created_at >= failed_cutoff)
        .scalar()
        or 0
    )

    reasons: list[str] = []
    if int(old_queued) > 0:
        reasons.append("critical_notifications_not_draining")
    if int(stuck_sending) > max_stuck_sending:
        reasons.append("critical_notifications_stuck_sending")
    if int(recent_failed) > max_failed:
        reasons.append("critical_notifications_failed")

    return EnforcementHealth(
        ok=not reasons,
        reasons=reasons,
        details={
            "old_queued": int(old_queued),
            "stuck_sending": int(stuck_sending),
            "recent_failed": int(recent_failed),
            "max_failed": max_failed,
            "max_stuck_sending": max_stuck_sending,
            "max_oldest_queued_minutes": max_oldest_minutes,
        },
    )


def payment_channel_health(db: Session) -> EnforcementHealth:
    """Return whether payment intake looks healthy enough to enforce."""
    now = datetime.now(UTC)
    window_hours = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_payment_health_window_hours",
        24,
    )
    stale_pending_minutes = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_payment_max_pending_minutes",
        45,
    )
    max_dead_letters = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_payment_max_dead_letters",
        0,
    )
    max_stale_pending = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_payment_max_stale_pending_topups",
        20,
    )
    min_recent_successes = _setting_int(
        db,
        SettingDomain.collections,
        "billing_enforcement_payment_min_recent_successes",
        0,
    )
    require_active_gateway = _setting_bool(
        db,
        SettingDomain.collections,
        "billing_enforcement_require_active_gateway",
        default=False,
    )
    window_cutoff = now - timedelta(hours=window_hours)
    stale_cutoff = now - timedelta(minutes=stale_pending_minutes)

    active_gateway_count = (
        db.query(func.count(PaymentProvider.id))
        .filter(PaymentProvider.is_active.is_(True))
        .filter(PaymentProvider.provider_type.in_(ONLINE_PAYMENT_PROVIDER_TYPES))
        .scalar()
        or 0
    )
    dead_letters = (
        db.query(func.count(PaymentWebhookDeadLetter.id))
        .filter(
            PaymentWebhookDeadLetter.status.in_(
                [
                    PaymentWebhookDeadLetterStatus.received,
                    PaymentWebhookDeadLetterStatus.failed,
                    PaymentWebhookDeadLetterStatus.rejected,
                    # Legacy replay marked rows resolved without proving that
                    # a Payment was posted. New successful replays delete the
                    # insurance row; retained ``replayed`` rows remain unsafe
                    # until they are reprocessed through the fixed owner.
                    PaymentWebhookDeadLetterStatus.replayed,
                ]
            )
        )
        .filter(PaymentWebhookDeadLetter.received_at >= window_cutoff)
        .scalar()
        or 0
    )
    stale_pending_topups = (
        db.query(func.count(TopupIntent.id))
        .filter(TopupIntent.status == "pending")
        .filter(TopupIntent.provider_type.in_(["paystack", "flutterwave"]))
        .filter(TopupIntent.created_at < stale_cutoff)
        .filter(
            or_(
                TopupIntent.expires_at.is_(None),
                TopupIntent.expires_at >= window_cutoff,
            )
        )
        .scalar()
        or 0
    )
    recent_successes = (
        db.query(func.count(Payment.id))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .filter(
            and_(
                Payment.paid_at.is_not(None),
                Payment.paid_at >= window_cutoff,
            )
        )
        .scalar()
        or 0
    )

    reasons: list[str] = []
    if require_active_gateway and int(active_gateway_count) <= 0:
        reasons.append("no_active_online_payment_gateway")
    if int(dead_letters) > max_dead_letters:
        reasons.append("payment_webhook_dead_letters")
    if int(stale_pending_topups) > max_stale_pending:
        reasons.append("stale_pending_topups")
    if int(recent_successes) < min_recent_successes:
        reasons.append("recent_payment_volume_below_floor")

    return EnforcementHealth(
        ok=not reasons,
        reasons=reasons,
        details={
            "active_gateway_count": int(active_gateway_count),
            "dead_letters": int(dead_letters),
            "stale_pending_topups": int(stale_pending_topups),
            "recent_successes": int(recent_successes),
            "max_dead_letters": max_dead_letters,
            "max_stale_pending_topups": max_stale_pending,
            "min_recent_successes": min_recent_successes,
        },
    )


def billing_enforcement_health(db: Session) -> EnforcementHealth:
    """Combined gate for service-affecting billing enforcement actions."""
    if not _setting_bool(
        db,
        SettingDomain.collections,
        "billing_enforcement_health_gates_enabled",
        default=True,
    ):
        return EnforcementHealth(ok=True, details={"health_gates_enabled": False})

    reasons: list[str] = []
    details: dict[str, int | bool] = {"health_gates_enabled": True}
    if _setting_bool(
        db,
        SettingDomain.collections,
        "billing_enforcement_require_notification_health",
        default=True,
    ):
        notification = notification_delivery_health(db)
        reasons.extend(notification.reasons)
        details.update(
            {f"notification_{k}": v for k, v in notification.details.items()}
        )
    if _setting_bool(
        db,
        SettingDomain.collections,
        "billing_enforcement_require_payment_health",
        default=True,
    ):
        payment = payment_channel_health(db)
        reasons.extend(payment.reasons)
        details.update({f"payment_{k}": v for k, v in payment.details.items()})

    return EnforcementHealth(ok=not reasons, reasons=reasons, details=details)
