"""Source of truth for customer and reseller communication decisions."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.notification import (
    CommunicationIntentRecord,
    CommunicationSuppression,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.schemas.notification import NotificationCreate
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_notification_policy import (
    resolve_subscriber_id_for_recipient,
)
from app.services.notification_channel_policy import resolve_notification_channels


class CommunicationClass(enum.StrEnum):
    transactional = "transactional"
    marketing = "marketing"
    operational = "operational"


@dataclass(frozen=True)
class CommunicationIntent:
    subscriber_id: UUID | None
    event_type: str
    category: str
    subject: str | None
    body: str | None
    template_id: UUID | None = None
    template_code: str | None = None
    communication_class: CommunicationClass = CommunicationClass.transactional
    default_channels: tuple[NotificationChannel, ...] = (NotificationChannel.email,)
    channels: tuple[NotificationChannel, ...] | None = None
    include_reseller: bool = True
    persist_policy_suppressions: bool = True
    subscriber_recipients: dict[NotificationChannel, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    dedupe_key: str | None = None
    send_at: datetime | None = None
    requested_status: NotificationStatus = NotificationStatus.queued


@dataclass(frozen=True)
class CommunicationIntentResult:
    intent_id: UUID
    deliveries: tuple[Notification, ...]
    queued: tuple[Notification, ...]
    suppressed: tuple[str, ...]


def _normalized_address(channel: NotificationChannel, value: str | None) -> str | None:
    if channel == NotificationChannel.email:
        return normalize_email_identifier(value)
    if channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        return normalize_phone_identifier(value)
    return (value or "").strip().lower() or None


def suppression_reason(
    db: Session,
    *,
    subscriber_id: UUID | None,
    channel: NotificationChannel,
    category: str | None,
    recipient: str | None,
    now: datetime | None = None,
) -> str | None:
    timestamp = now or datetime.now(UTC)
    normalized = _normalized_address(channel, recipient)
    query = db.query(CommunicationSuppression).filter(
        CommunicationSuppression.is_active.is_(True),
        or_(
            CommunicationSuppression.expires_at.is_(None),
            CommunicationSuppression.expires_at > timestamp,
        ),
    )
    if subscriber_id is not None and normalized:
        query = query.filter(
            or_(
                CommunicationSuppression.subscriber_id == subscriber_id,
                CommunicationSuppression.normalized_address == normalized,
            )
        )
    elif subscriber_id is not None:
        query = query.filter(CommunicationSuppression.subscriber_id == subscriber_id)
    elif normalized:
        query = query.filter(CommunicationSuppression.normalized_address == normalized)
    else:
        return None
    query = query.filter(
        or_(
            CommunicationSuppression.channel.is_(None),
            CommunicationSuppression.channel == channel,
        ),
        or_(
            CommunicationSuppression.category.is_(None),
            CommunicationSuppression.category == category,
        ),
    )
    row = query.order_by(CommunicationSuppression.created_at.desc()).first()
    return row.reason if row is not None else None


def suppress(
    db: Session,
    *,
    reason: str,
    source: str,
    subscriber_id: UUID | None = None,
    channel: NotificationChannel | None = None,
    category: str | None = None,
    address: str | None = None,
    expires_at: datetime | None = None,
) -> CommunicationSuppression:
    if subscriber_id is None and not address:
        raise ValueError("subscriber_id or address is required")
    normalized = _normalized_address(channel, address) if channel else None
    existing = (
        db.query(CommunicationSuppression)
        .filter(CommunicationSuppression.subscriber_id == subscriber_id)
        .filter(CommunicationSuppression.channel == channel)
        .filter(CommunicationSuppression.category == category)
        .filter(CommunicationSuppression.normalized_address == normalized)
        .filter(CommunicationSuppression.is_active.is_(True))
        .one_or_none()
    )
    if existing is not None:
        existing.reason = reason
        existing.source = source
        existing.expires_at = expires_at
        db.flush()
        return existing
    row = CommunicationSuppression(
        subscriber_id=subscriber_id,
        channel=channel,
        category=category,
        normalized_address=normalized,
        reason=reason,
        source=source,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    return row


def unsuppress(db: Session, suppression_id: UUID) -> None:
    row = db.get(CommunicationSuppression, suppression_id)
    if row is None:
        raise ValueError("Communication suppression not found")
    row.is_active = False
    db.flush()


def suppress_committed(
    db: Session,
    *,
    reason: str,
    source: str,
    subscriber_id: UUID | None = None,
    channel: NotificationChannel | None = None,
    category: str | None = None,
    address: str | None = None,
    expires_at: datetime | None = None,
) -> CommunicationSuppression:
    row = suppress(
        db,
        reason=reason,
        source=source,
        subscriber_id=subscriber_id,
        channel=channel,
        category=category,
        address=address,
        expires_at=expires_at,
    )
    db.commit()
    db.refresh(row)
    return row


def unsuppress_committed(db: Session, suppression_id: UUID) -> None:
    unsuppress(db, suppression_id)
    db.commit()


def list_intents(
    db: Session,
    *,
    subscriber_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CommunicationIntentRecord]:
    query = db.query(CommunicationIntentRecord)
    if subscriber_id is not None:
        query = query.filter(CommunicationIntentRecord.subscriber_id == subscriber_id)
    if status:
        query = query.filter(CommunicationIntentRecord.status == status)
    return list(
        query.order_by(CommunicationIntentRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def list_suppressions(
    db: Session,
    *,
    subscriber_id: UUID | None = None,
    is_active: bool | None = True,
    limit: int = 50,
    offset: int = 0,
) -> list[CommunicationSuppression]:
    query = db.query(CommunicationSuppression)
    if subscriber_id is not None:
        query = query.filter(CommunicationSuppression.subscriber_id == subscriber_id)
    if is_active is not None:
        query = query.filter(CommunicationSuppression.is_active == is_active)
    return list(
        query.order_by(CommunicationSuppression.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _subscriber_address(
    subscriber: Subscriber, channel: NotificationChannel
) -> str | None:
    if channel == NotificationChannel.email:
        return subscriber.email
    if channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        return subscriber.phone
    if channel == NotificationChannel.push:
        return str(subscriber.id)
    return None


def _reseller_addresses(
    db: Session, reseller: Reseller, channel: NotificationChannel
) -> list[str]:
    addresses: list[str] = []
    if channel == NotificationChannel.email and reseller.contact_email:
        addresses.append(reseller.contact_email)
    elif channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        if reseller.contact_phone:
            addresses.append(reseller.contact_phone)
    if channel == NotificationChannel.email:
        addresses.extend(
            email
            for (email,) in db.query(ResellerUser.email)
            .filter(ResellerUser.reseller_id == reseller.id)
            .filter(ResellerUser.is_active.is_(True))
            .filter(ResellerUser.email.is_not(None))
            .all()
            if email
        )
    return list(dict.fromkeys(address.strip() for address in addresses if address))


def submit(db: Session, intent: CommunicationIntent) -> CommunicationIntentResult:
    from app.services.notification import notifications as notification_service

    resolved_subscriber_id = intent.subscriber_id
    if resolved_subscriber_id is None:
        for identity_recipient in intent.subscriber_recipients.values():
            resolved_subscriber_id = resolve_subscriber_id_for_recipient(
                db, identity_recipient
            )
            if resolved_subscriber_id is not None:
                break
    subscriber = (
        db.get(Subscriber, resolved_subscriber_id) if resolved_subscriber_id else None
    )
    if intent.subscriber_id is not None and subscriber is None:
        raise ValueError("Subscriber not found")
    channels = intent.channels or resolve_notification_channels(
        db,
        template_code=intent.template_code,
        event_type=intent.event_type,
        category=intent.category,
        default_channels=intent.default_channels,
    )
    if intent.dedupe_key:
        existing = (
            db.query(CommunicationIntentRecord)
            .filter(CommunicationIntentRecord.dedupe_key == intent.dedupe_key)
            .one_or_none()
        )
        if existing is not None:
            return CommunicationIntentResult(
                intent_id=existing.id,
                deliveries=tuple(existing.notifications),
                queued=tuple(
                    item
                    for item in existing.notifications
                    if item.status == NotificationStatus.queued
                ),
                suppressed=tuple(existing.suppression_reasons or []),
            )

    record = CommunicationIntentRecord(
        subscriber_id=subscriber.id if subscriber else None,
        event_type=intent.event_type,
        category=intent.category,
        communication_class=intent.communication_class.value,
        template_id=intent.template_id,
        template_code=intent.template_code,
        subject=intent.subject,
        body=intent.body,
        channels=[channel.value for channel in channels],
        include_reseller=intent.include_reseller,
        status="pending",
        suppression_reasons=[],
        dedupe_key=intent.dedupe_key,
        scheduled_for=intent.send_at,
        metadata_=dict(intent.metadata),
    )
    db.add(record)
    db.flush()

    suppressed: list[str] = []
    if intent.communication_class == CommunicationClass.marketing and (
        subscriber is None or not subscriber.marketing_opt_in
    ):
        record.status = "suppressed"
        record.suppression_reasons = ["marketing_opt_out"]
        record.processed_at = datetime.now(UTC)
        db.flush()
        return CommunicationIntentResult(
            intent_id=record.id,
            deliveries=(),
            queued=(),
            suppressed=("marketing_opt_out",),
        )

    queued: list[Notification] = []
    for channel in channels:
        delivery_recipient = intent.subscriber_recipients.get(channel) or (
            _subscriber_address(subscriber, channel) if subscriber else None
        )
        if not delivery_recipient:
            suppressed.append(f"subscriber:{channel.value}:missing_address")
        else:
            reason = suppression_reason(
                db,
                subscriber_id=subscriber.id if subscriber else None,
                channel=channel,
                category=intent.category,
                recipient=delivery_recipient,
            )
            if reason:
                suppressed.append(f"subscriber:{channel.value}:{reason}")
                if not intent.persist_policy_suppressions:
                    continue
            if not reason or intent.persist_policy_suppressions:
                payload = NotificationCreate(
                    template_id=intent.template_id,
                    subscriber_id=subscriber.id if subscriber else None,
                    communication_intent_id=record.id,
                    audience_type="subscriber",
                    audience_id=subscriber.id if subscriber else None,
                    channel=channel,
                    event_type=intent.event_type,
                    category=intent.category,
                    recipient=delivery_recipient,
                    subject=intent.subject,
                    body=intent.body,
                    status=intent.requested_status,
                    send_at=intent.send_at,
                    metadata_=dict(intent.metadata),
                )
                notification = (
                    notification_service.queue_customer_notification(db, payload)
                    if intent.persist_policy_suppressions
                    else notification_service.queue_event_notification(db, payload)
                )
                if notification is None:
                    suppressed.append(f"subscriber:{channel.value}:customer_policy")
                else:
                    queued.append(notification)

        reseller = subscriber.reseller if subscriber else None
        if (
            not intent.include_reseller
            or reseller is None
            or reseller.is_house
            or not reseller.is_active
        ):
            continue
        for reseller_recipient in _reseller_addresses(db, reseller, channel):
            reseller_reason = suppression_reason(
                db,
                subscriber_id=None,
                channel=channel,
                category=intent.category,
                recipient=reseller_recipient,
            )
            if reseller_reason:
                suppressed.append(f"reseller:{channel.value}:{reseller_reason}")
                continue
            queued.append(
                notification_service.queue_internal_notification(
                    db,
                    NotificationCreate(
                        template_id=intent.template_id,
                        communication_intent_id=record.id,
                        audience_type="reseller",
                        audience_id=reseller.id,
                        channel=channel,
                        event_type=intent.event_type,
                        category=intent.category,
                        recipient=reseller_recipient,
                        subject=intent.subject,
                        body=intent.body,
                        status=intent.requested_status,
                        send_at=intent.send_at,
                        metadata_={
                            **intent.metadata,
                            "subject_subscriber_id": str(subscriber.id)
                            if subscriber
                            else None,
                        },
                    ),
                )
            )
    active_queued = any(
        notification.status == NotificationStatus.queued for notification in queued
    )
    record.status = (
        "partial"
        if active_queued and suppressed
        else "expanded"
        if active_queued
        else "suppressed"
    )
    record.suppression_reasons = suppressed
    record.processed_at = datetime.now(UTC)
    db.flush()
    return CommunicationIntentResult(
        intent_id=record.id,
        deliveries=tuple(queued),
        queued=tuple(
            item for item in queued if item.status == NotificationStatus.queued
        ),
        suppressed=tuple(suppressed),
    )


def record_delivery_outcome(db: Session, notification: Notification) -> None:
    """Project outbox delivery state into its intent, campaign, and inbox lineage."""
    from app.models.comms_campaign import (
        CampaignRecipient,
        CampaignRecipientStatus,
    )
    from app.models.team_inbox import InboxMessage

    db.flush()

    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.notification_id == notification.id)
        .one_or_none()
    )
    if message is not None:
        metadata = dict(message.metadata_ or {})
        metadata["delivery_status"] = notification.status.value
        if notification.last_error:
            metadata["send_error"] = notification.last_error
        else:
            metadata.pop("send_error", None)
        message.metadata_ = metadata
        if notification.status == NotificationStatus.delivered:
            message.sent_at = notification.sent_at or datetime.now(UTC)

    campaign_recipient = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.notification_id == notification.id)
        .one_or_none()
    )
    if campaign_recipient is not None:
        if notification.status == NotificationStatus.delivered:
            campaign_recipient.status = CampaignRecipientStatus.delivered.value
            campaign_recipient.delivered_at = notification.sent_at or datetime.now(UTC)
            campaign_recipient.failed_reason = None
        elif (
            notification.status == NotificationStatus.failed
            and notification.send_at is None
        ):
            campaign_recipient.status = CampaignRecipientStatus.failed.value
            campaign_recipient.failed_reason = notification.last_error
        elif notification.status == NotificationStatus.canceled:
            campaign_recipient.status = CampaignRecipientStatus.skipped.value
            campaign_recipient.failed_reason = notification.last_error
        from app.services.comms_campaigns import refresh_campaign_delivery_state

        refresh_campaign_delivery_state(db, campaign_recipient.campaign_id)

    if notification.communication_intent_id is None:
        return
    intent_record = db.get(
        CommunicationIntentRecord, notification.communication_intent_id
    )
    if intent_record is None:
        return
    delivery_rows = (
        db.query(Notification.status, Notification.send_at)
        .filter(Notification.communication_intent_id == intent_record.id)
        .all()
    )
    states = {status for status, _send_at in delivery_rows}
    if states & {NotificationStatus.queued, NotificationStatus.sending}:
        intent_record.status = "delivering"
    elif states and states <= {NotificationStatus.delivered}:
        intent_record.status = "delivered"
    elif any(
        status == NotificationStatus.failed and send_at is not None
        for status, send_at in delivery_rows
    ):
        intent_record.status = "retrying"
    elif NotificationStatus.delivered in states:
        intent_record.status = "partial"
    elif states:
        intent_record.status = "failed"
    intent_record.updated_at = datetime.now(UTC)
    db.flush()
