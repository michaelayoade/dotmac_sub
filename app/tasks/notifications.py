"""Celery tasks for notification delivery."""

import json
import logging
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services import email as email_service
from app.services import push as push_service
from app.services import sms as sms_service
from app.services.db_session_adapter import db_session_adapter
from app.services.email_template import render_email_bodies
from app.services.integrations.connectors import whatsapp as whatsapp_service
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

# Timeout before a "sending" notification is treated as stuck and reclaimed.
# Kept comfortably longer than normal provider latency so a slow-but-live send
# is not reclaimed (and re-sent) out from under us.
SENDING_TIMEOUT_MINUTES = 10
# Maximum delivery retries before marking as permanently failed
MAX_RETRIES = 3
# Default max age before a still-undelivered notification is expired instead
# of sent (guards against draining weeks of stale dunning when the queue
# runner is re-enabled). 0 disables expiry.
DEFAULT_MAX_QUEUE_AGE_HOURS = 72

# Per-channel reclaim policy for notifications stuck in "sending" (the worker
# may have crashed AFTER handing the message to the provider but BEFORE the
# status commit, so re-sending risks a duplicate). We have no provider-side
# idempotency key yet, so the policy is content-driven:
#   - at_most_once: noisy/low-value bulk (a duplicate blast harms sender
#     reputation and the message is disposable) — do NOT re-send a stuck one.
#   - at_least_once: everything else — critical transactional notices
#     (billing/service/account/auth) where silently losing one is worse than a
#     rare duplicate; re-send, but bounded by MAX_RETRIES.
# A duplicate is the lesser evil for criticals; loss is the lesser evil for
# bulk. True exactly-once (provider idempotency keys) is a follow-up.
_AT_MOST_ONCE_CATEGORIES = frozenset({"general"})
_AT_MOST_ONCE_EVENT_TYPES = frozenset({"service_bulk_message"})


def _reclaim_policy(notification) -> str:
    """Return 'at_most_once' for noisy/bulk notifications, else 'at_least_once'."""
    if (notification.category or "") in _AT_MOST_ONCE_CATEGORIES:
        return "at_most_once"
    if (notification.event_type or "") in _AT_MOST_ONCE_EVENT_TYPES:
        return "at_most_once"
    return "at_least_once"


def _max_queue_age_hours(db) -> int:
    value = resolve_value(
        db, SettingDomain.notification, "notification_max_queue_age_hours"
    )
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUEUE_AGE_HOURS


def _expire_stale_notifications(db, now) -> int:
    """Cancel undelivered notifications that have sat in the queue too long."""
    max_age_hours = _max_queue_age_hours(db)
    if max_age_hours <= 0:
        return 0
    cutoff = now - timedelta(hours=max_age_hours)
    expired = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.status.in_(
                [
                    NotificationStatus.queued,
                    NotificationStatus.sending,
                    NotificationStatus.failed,
                ]
            )
        )
        .filter(Notification.created_at < cutoff)
        # Deliberately future-scheduled sends are expired only once their
        # send_at is itself past the cutoff.
        .filter((Notification.send_at.is_(None)) | (Notification.send_at < cutoff))
        .update(
            {
                "status": NotificationStatus.canceled,
                "last_error": "expired_in_queue",
            },
            synchronize_session=False,
        )
    )
    if expired:
        db.commit()
        logger.info(
            "Expired %d stale notifications older than %dh", expired, max_age_hours
        )
    return int(expired or 0)


def _deliver_notification_queue_stats(db, batch_size: int = 50) -> dict[str, int]:
    now = datetime.now(UTC)
    stuck_threshold = now - timedelta(minutes=SENDING_TIMEOUT_MINUTES)

    expired = _expire_stale_notifications(db, now)

    # Query queued, stuck "sending", and retryable failed notifications
    notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.channel.in_(
                [
                    NotificationChannel.email,
                    NotificationChannel.sms,
                    NotificationChannel.whatsapp,
                    NotificationChannel.push,
                ]
            )
        )
        .filter(
            or_(
                # Queued notifications ready to send
                Notification.status == NotificationStatus.queued,
                # Stuck "sending" notifications (likely crashed during send)
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_threshold)
                ),
                # Failed notifications eligible for retry (under max retries)
                (
                    (Notification.status == NotificationStatus.failed)
                    & (Notification.retry_count < MAX_RETRIES)
                ),
            )
        )
        .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .all()
    )
    delivered = 0
    retried = 0
    failed = 0
    reclaimed = 0
    stuck_dropped = 0
    for notification in notifications:
        # Reclaim handling: a notification still in "sending" was stuck past the
        # timeout — the worker likely crashed mid-send, possibly AFTER the
        # provider was already called. Apply the per-channel reclaim policy
        # before re-handing it to the provider.
        if notification.status == NotificationStatus.sending:
            notification.retry_count = (notification.retry_count or 0) + 1
            if _reclaim_policy(notification) == "at_most_once":
                # No provider-side dedupe for bulk; a duplicate blast is worse
                # than dropping a disposable message — do not re-send.
                notification.status = NotificationStatus.failed
                notification.last_error = "stuck_sending_not_resent (at-most-once)"
                stuck_dropped += 1
                db.commit()
                continue
            if notification.retry_count > MAX_RETRIES:
                notification.status = NotificationStatus.failed
                notification.last_error = "stuck_sending_reclaim_exhausted"
                failed += 1
                logger.warning(
                    "Notification %s reclaim exhausted after %d attempts; giving up",
                    notification.id,
                    notification.retry_count,
                )
                db.commit()
                continue
            reclaimed += 1
            logger.info(
                "Reclaiming stuck-sending notification %s "
                "(at-least-once, attempt %d/%d)",
                notification.id,
                notification.retry_count,
                MAX_RETRIES,
            )
        # Update status before sending
        notification.status = NotificationStatus.sending
        db.commit()

        subject = notification.subject or "Notification"
        body = notification.body or ""
        try:
            if notification.channel == NotificationChannel.email:
                # Queue bodies are usually plain text — wrap them in the
                # branded template and keep the text as the text/plain part.
                body_html, body_text = render_email_bodies(body, subject=subject)
                success = email_service.send_email(
                    db=db,
                    to_email=notification.recipient,
                    subject=subject,
                    body_html=body_html,
                    body_text=body_text,
                    track=False,
                    activity="notification_queue",
                    notification_id=str(notification.id),
                )
            elif notification.channel == NotificationChannel.sms:
                success = sms_service.send_sms(
                    db=db,
                    to_phone=notification.recipient,
                    body=body,
                    track=False,
                    notification_id=str(notification.id),
                )
            elif notification.channel == NotificationChannel.whatsapp:
                whatsapp_payload = None
                if body:
                    try:
                        parsed_body = json.loads(body)
                        if isinstance(parsed_body, dict) and parsed_body.get(
                            "__whatsapp_template__"
                        ):
                            whatsapp_payload = parsed_body
                    except json.JSONDecodeError:
                        whatsapp_payload = None
                if whatsapp_payload:
                    result = whatsapp_service.send_template_message(
                        db=db,
                        recipient=notification.recipient,
                        template_name=str(whatsapp_payload.get("name") or ""),
                        language=str(whatsapp_payload.get("language") or "") or None,
                        variables=whatsapp_payload.get("variables") or {},
                        dry_run=False,
                    )
                elif notification.template:
                    result = whatsapp_service.send_template_message(
                        db=db,
                        recipient=notification.recipient,
                        template_name=notification.template.code,
                        variables={},
                        dry_run=False,
                    )
                else:
                    result = whatsapp_service.send_text_message(
                        db=db,
                        recipient=notification.recipient,
                        body=body,
                        dry_run=False,
                    )
                success = bool(result.get("ok"))
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider=str(result.get("provider") or "whatsapp"),
                        provider_message_id=None,
                        status=DeliveryStatus.delivered
                        if success
                        else DeliveryStatus.failed,
                        response_code=str(result.get("status_code") or ""),
                        response_body=str(
                            result.get("response") or result.get("message") or ""
                        )
                        or None,
                    )
                )
                if not success:
                    notification.last_error = str(
                        result.get("response") or "whatsapp_send_failed"
                    )
            elif notification.channel == NotificationChannel.push:
                success = push_service.send_push(
                    db=db,
                    subscriber_id=notification.subscriber_id,
                    title=subject,
                    body=body,
                    notification_id=str(notification.id),
                )
            else:
                success = False
                notification.last_error = (
                    f"unsupported_channel:{notification.channel.value}"
                )
        except Exception as exc:
            success = False
            notification.last_error = str(exc)

        if success:
            notification.status = NotificationStatus.delivered
            notification.sent_at = datetime.now(UTC)
            notification.last_error = None
            delivered += 1
        else:
            notification.retry_count = (notification.retry_count or 0) + 1
            if notification.retry_count >= MAX_RETRIES:
                notification.status = NotificationStatus.failed
                failed += 1
                logger.warning(
                    "Notification %s permanently failed after %d retries: %s",
                    notification.id,
                    notification.retry_count,
                    notification.last_error,
                )
            else:
                # Schedule for retry — set back to failed, will be picked up next run
                notification.status = NotificationStatus.failed
                retried += 1
                logger.info(
                    "Notification %s retry %d/%d scheduled",
                    notification.id,
                    notification.retry_count,
                    MAX_RETRIES,
                )
            if not notification.last_error:
                notification.last_error = "send_failed"
        db.commit()

    return {
        "delivered": delivered,
        "retried": retried,
        "failed": failed,
        "expired": expired,
        "reclaimed": reclaimed,
        "stuck_dropped": stuck_dropped,
    }


def _deliver_notification_queue(db, batch_size: int = 50) -> int:
    return _deliver_notification_queue_stats(db, batch_size=batch_size)["delivered"]


@celery_app.task(name="app.tasks.notifications.deliver_notification_queue")
def deliver_notification_queue() -> dict[str, int]:
    """Process queued notifications and retry failed ones."""
    with db_session_adapter.session() as session:
        result = _deliver_notification_queue_stats(session)
        logger.info(
            "Notification queue processed: delivered=%d, retried=%d, failed=%d, "
            "expired=%d",
            result["delivered"],
            result["retried"],
            result["failed"],
            result["expired"],
        )
        return result
