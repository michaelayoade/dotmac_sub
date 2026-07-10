from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalRoomProvider,
)
from app.models.service_team import ServiceTeamMember
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryTarget:
    recipient_type: str
    recipient_id: str | None
    address: str


def dispatch_pending_deliveries(
    db: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[OperationalEscalationDelivery]:
    deliveries = (
        db.query(OperationalEscalationDelivery)
        .join(OperationalEscalationEvent)
        .filter(
            OperationalEscalationDelivery.delivery_status
            == OperationalDeliveryStatus.pending
        )
        .order_by(OperationalEscalationDelivery.created_at.asc())
        .limit(limit)
        .all()
    )
    return [dispatch_delivery(db, delivery, now=now) for delivery in deliveries]


def dispatch_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    now: datetime | None = None,
) -> OperationalEscalationDelivery:
    timestamp = now or datetime.now(UTC)
    event = delivery.event
    if event.status != OperationalEscalationStatus.open:
        return _suppress_delivery(
            db,
            delivery,
            reason=f"event.{event.status}",
            timestamp=timestamp,
        )

    try:
        targets = _delivery_targets(db, delivery)
        if not targets:
            return _fail_delivery(db, delivery, "No delivery target", timestamp)

        results = [
            _send_to_target(db, delivery=delivery, target=target) for target in targets
        ]
        metadata = {
            **(delivery.metadata_ or {}),
            "dispatch_results": results,
            "dispatched_at": timestamp.isoformat(),
        }
        delivery.metadata_ = metadata
        if all(result.get("ok") for result in results):
            delivery.delivery_status = OperationalDeliveryStatus.sent
            delivery.sent_at = timestamp
            delivery.error_message = None
        else:
            errors = [
                str(result.get("error") or result.get("message") or "send_failed")
                for result in results
                if not result.get("ok")
            ]
            return _fail_delivery(
                db,
                delivery,
                "; ".join(errors) or "Delivery failed",
                timestamp,
            )
        db.flush()
        return delivery
    except Exception as exc:
        logger.warning(
            "operational_escalation_delivery_failed delivery_id=%s error=%s",
            delivery.id,
            exc,
        )
        return _fail_delivery(db, delivery, str(exc), timestamp)


def _send_to_target(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    target: DeliveryTarget,
) -> dict[str, Any]:
    channel = delivery.channel
    title, body = _delivery_title_body(delivery)
    if channel == OperationalNotificationChannel.whatsapp:
        from app.services.integrations.connectors import whatsapp as whatsapp_connector

        whatsapp_result = whatsapp_connector.send_text_message(
            db,
            recipient=target.address,
            body=body,
            dry_run=False,
        )
        return {
            "ok": bool(whatsapp_result.get("ok")),
            "channel": channel,
            "recipient": target.address,
            "provider": whatsapp_result.get("provider"),
            "response": whatsapp_result.get("response"),
            "status_code": whatsapp_result.get("status_code"),
        }

    if channel == OperationalNotificationChannel.push:
        return _send_push(db, delivery=delivery, target=target, title=title, body=body)

    if channel == OperationalNotificationChannel.nextcloud_talk:
        return _send_nextcloud_talk(db, delivery=delivery, message=body)

    adapter_channel = (
        "websocket" if channel == OperationalNotificationChannel.web else channel
    )
    from app.services.notification_adapter import send_notification

    notification_result = send_notification(
        adapter_channel,
        target.address,
        body,
        title=title,
        subject=title,
        metadata=_delivery_metadata(delivery),
        idempotency_key=delivery.dedup_key,
    )
    return {
        "ok": bool(notification_result.success),
        "channel": channel,
        "recipient": target.address,
        "message": notification_result.message,
        "status": notification_result.status.value,
        "error": notification_result.error,
    }


def _send_push(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    target: DeliveryTarget,
    title: str,
    body: str,
) -> dict[str, Any]:
    from app.services import push

    if target.recipient_type == OperationalParticipantType.subscriber:
        ok = push.send_push(
            db,
            target.address,
            title,
            body,
            data=_delivery_metadata(delivery),
            notification_id=str(delivery.id),
        )
        return {
            "ok": ok,
            "channel": OperationalNotificationChannel.push,
            "recipient": target.address,
        }
    if target.recipient_type == OperationalParticipantType.person:
        ok = push.send_push_to_system_user(
            db,
            target.address,
            title,
            body,
            data=_delivery_metadata(delivery),
            notification_id=str(delivery.id),
        )
        return {
            "ok": ok,
            "channel": OperationalNotificationChannel.push,
            "recipient": target.address,
        }
    return {
        "ok": False,
        "channel": OperationalNotificationChannel.push,
        "recipient": target.address,
        "error": "Push requires subscriber or person recipient",
    }


def _send_nextcloud_talk(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    message: str,
) -> dict[str, Any]:
    from app.services.nextcloud_talk import resolve_talk_client

    metadata = _delivery_metadata(delivery)
    room_token = (
        delivery.recipient_address
        or metadata.get("room_token")
        or _entity_room_token(db, delivery.event)
    )
    if not room_token:
        return {
            "ok": False,
            "channel": OperationalNotificationChannel.nextcloud_talk,
            "error": "No Nextcloud Talk room token",
        }
    client = resolve_talk_client(
        db,
        base_url=metadata.get("nextcloud_base_url"),
        username=metadata.get("nextcloud_username"),
        app_password=metadata.get("nextcloud_app_password"),
        timeout_sec=metadata.get("nextcloud_timeout_sec"),
        connector_config_id=metadata.get("nextcloud_connector_config_id"),
    )
    response = client.post_message(str(room_token), message)
    return {
        "ok": True,
        "channel": OperationalNotificationChannel.nextcloud_talk,
        "recipient": str(room_token),
        "response": response,
    }


def _delivery_targets(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if delivery.recipient_address:
        return [
            DeliveryTarget(
                recipient_type=delivery.recipient_type,
                recipient_id=delivery.recipient_id,
                address=delivery.recipient_address,
            )
        ]

    if delivery.recipient_type == OperationalParticipantType.team:
        return _team_targets(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.person:
        return _person_target(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.subscriber:
        return _subscriber_target(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.external:
        address = _delivery_metadata(delivery).get("recipient_address")
        return (
            [
                DeliveryTarget(
                    recipient_type=delivery.recipient_type,
                    recipient_id=delivery.recipient_id,
                    address=str(address),
                )
            ]
            if address
            else []
        )
    if delivery.recipient_type == OperationalParticipantType.duty_role:
        return _duty_role_targets(delivery)
    return []


def _team_targets(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    members = (
        db.query(ServiceTeamMember)
        .filter(ServiceTeamMember.team_id == _uuid(delivery.recipient_id))
        .filter(ServiceTeamMember.is_active.is_(True))
        .all()
    )
    targets: list[DeliveryTarget] = []
    for member in members:
        targets.extend(
            _target_for_system_user(
                db,
                person_id=str(member.person_id),
                channel=delivery.channel,
            )
        )
    return targets


def _person_target(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    return _target_for_system_user(
        db,
        person_id=delivery.recipient_id,
        channel=delivery.channel,
    )


def _target_for_system_user(
    db: Session,
    *,
    person_id: str,
    channel: str,
) -> list[DeliveryTarget]:
    user = db.get(SystemUser, _uuid(person_id))
    if user is None or not user.is_active:
        return []
    if channel == OperationalNotificationChannel.email and user.email:
        address = user.email
    elif (
        channel
        in {
            OperationalNotificationChannel.sms,
            OperationalNotificationChannel.whatsapp,
        }
        and user.phone
    ):
        address = user.phone
    elif channel in {
        OperationalNotificationChannel.web,
        OperationalNotificationChannel.push,
    }:
        address = str(user.id)
    else:
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.person,
            recipient_id=str(user.id),
            address=address,
        )
    ]


def _subscriber_target(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    subscriber = db.get(Subscriber, _uuid(delivery.recipient_id))
    if subscriber is None:
        return []
    if delivery.channel == OperationalNotificationChannel.email and subscriber.email:
        address = subscriber.email
    elif (
        delivery.channel
        in {
            OperationalNotificationChannel.sms,
            OperationalNotificationChannel.whatsapp,
        }
        and subscriber.phone
    ):
        address = subscriber.phone
    elif delivery.channel in {
        OperationalNotificationChannel.web,
        OperationalNotificationChannel.push,
    }:
        address = str(subscriber.id)
    else:
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.subscriber,
            recipient_id=str(subscriber.id),
            address=address,
        )
    ]


def _duty_role_targets(delivery: OperationalEscalationDelivery) -> list[DeliveryTarget]:
    role_channels = _delivery_metadata(delivery).get("duty_role_channels")
    if not isinstance(role_channels, dict):
        return []
    channel_targets = role_channels.get(delivery.channel)
    if isinstance(channel_targets, str):
        channel_targets = [channel_targets]
    if not isinstance(channel_targets, list):
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.duty_role,
            recipient_id=delivery.recipient_id,
            address=str(address),
        )
        for address in channel_targets
        if address
    ]


def _entity_room_token(db: Session, event: OperationalEscalationEvent) -> str | None:
    link = (
        db.query(OperationalRoomLink)
        .filter(OperationalRoomLink.entity_type == event.entity_type)
        .filter(OperationalRoomLink.entity_id == event.entity_id)
        .filter(OperationalRoomLink.provider == OperationalRoomProvider.nextcloud_talk)
        .filter(OperationalRoomLink.is_active.is_(True))
        .order_by(OperationalRoomLink.created_at.desc())
        .first()
    )
    if link is None:
        return None
    metadata = link.metadata_ or {}
    return str(metadata.get("room_token") or link.room_id or "")


def _delivery_title_body(delivery: OperationalEscalationDelivery) -> tuple[str, str]:
    metadata = _delivery_metadata(delivery)
    title = str(
        metadata.get("title")
        or metadata.get("subject")
        or f"Operational escalation: {delivery.event.entity_type}"
    )
    body = str(
        metadata.get("body") or metadata.get("message") or _default_body(delivery)
    )
    return title, body


def _default_body(delivery: OperationalEscalationDelivery) -> str:
    event = delivery.event
    return (
        f"Escalation level {event.level}: {event.trigger} "
        f"for {event.entity_type} {event.entity_id}"
    )


def _delivery_metadata(delivery: OperationalEscalationDelivery) -> dict[str, Any]:
    event_metadata = (
        delivery.event.metadata_ if isinstance(delivery.event.metadata_, dict) else {}
    )
    delivery_metadata = (
        delivery.metadata_ if isinstance(delivery.metadata_, dict) else {}
    )
    return {
        **event_metadata,
        **delivery_metadata,
        "delivery_id": str(delivery.id),
        "event_id": str(delivery.event_id),
        "entity_type": delivery.event.entity_type,
        "entity_id": delivery.event.entity_id,
        "trigger": delivery.event.trigger,
        "level": delivery.event.level,
    }


def _suppress_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    reason: str,
    timestamp: datetime,
) -> OperationalEscalationDelivery:
    delivery.delivery_status = OperationalDeliveryStatus.suppressed
    delivery.error_message = reason
    delivery.metadata_ = {
        **(delivery.metadata_ or {}),
        "suppressed_reason": reason,
        "suppressed_at": timestamp.isoformat(),
    }
    db.flush()
    return delivery


def _fail_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    error: str,
    timestamp: datetime,
) -> OperationalEscalationDelivery:
    delivery.delivery_status = OperationalDeliveryStatus.failed
    delivery.error_message = error
    delivery.metadata_ = {
        **(delivery.metadata_ or {}),
        "failed_at": timestamp.isoformat(),
    }
    db.flush()
    return delivery


def _uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
