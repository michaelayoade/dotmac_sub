from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxContactLink,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_media, team_inbox_routing
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_channel_address,
)
from app.services.integrations.connectors import whatsapp as whatsapp_connector

_INACTIVE_SUBSCRIBER_STATUSES = {
    SubscriberStatus.disabled.value,
    SubscriberStatus.canceled.value,
}
_OPAQUE_CONTACT_CHANNELS = {
    InboxChannelType.facebook_messenger.value,
    InboxChannelType.instagram_dm.value,
    InboxChannelType.chat_widget.value,
}


@dataclass(frozen=True)
class InboundChannelPayload:
    channel_type: str
    contact_address: str
    body: str
    contact_name: str | None = None
    external_message_id: str | None = None
    external_thread_id: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    subscriber_id: str | UUID | None = None
    fallback_service_team_id: str | UUID | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class InboundChannelReceiveResult:
    kind: str
    conversation_id: str
    message_id: str
    duplicate: bool
    subscriber_id: str | None = None
    reseller_id: str | None = None
    resolution_status: str = "unmatched"


@dataclass(frozen=True)
class ContactResolution:
    status: str
    normalized_contact: str | None
    subscriber_id: UUID | None
    reseller_id: UUID | None
    matched_subscriber_ids: list[str]
    suppressed_subscriber_ids: list[str]
    matched_reseller_ids: list[str]

    def as_metadata(self) -> dict[str, object]:
        return {
            "status": self.status,
            "normalized_contact": self.normalized_contact,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "reseller_id": str(self.reseller_id) if self.reseller_id else None,
            "matched_subscriber_ids": self.matched_subscriber_ids,
            "suppressed_subscriber_ids": self.suppressed_subscriber_ids,
            "matched_reseller_ids": self.matched_reseller_ids,
        }


def _status_value(subscriber: Subscriber) -> str:
    return str(getattr(subscriber.status, "value", subscriber.status) or "")


def _subscriber_is_linkable(subscriber: Subscriber) -> bool:
    return (
        bool(subscriber.is_active)
        and _status_value(subscriber) not in _INACTIVE_SUBSCRIBER_STATUSES
    )


def _normalize_contact(db: Session, channel_type: str, value: str | None) -> str | None:
    return _normalize_contact_with_country(
        channel_type,
        value,
        country_code=default_country_code(db),
    )


def _normalize_contact_with_country(
    channel_type: str,
    value: str | None,
    *,
    country_code: str,
) -> str | None:
    if channel_type in _OPAQUE_CONTACT_CHANNELS:
        normalized = str(value or "").strip()
        return normalized or None
    return normalize_channel_address(
        channel_type,
        value,
        default_country_code=country_code,
    )


def _subscriber_contact(subscriber: Subscriber, channel_type: str) -> str | None:
    if channel_type == InboxChannelType.email.value:
        return subscriber.email
    return subscriber.phone


def _reseller_contact(reseller: Reseller, channel_type: str) -> str | None:
    if channel_type == InboxChannelType.email.value:
        return reseller.contact_email
    return reseller.contact_phone


def resolve_contact_context(
    db: Session,
    *,
    channel_type: str,
    contact_address: str,
    subscriber_id: str | UUID | None = None,
) -> ContactResolution:
    country_code = default_country_code(db)
    normalized = _normalize_contact_with_country(
        channel_type, contact_address, country_code=country_code
    )
    explicit_subscriber_id = coerce_uuid(subscriber_id)
    if explicit_subscriber_id is not None:
        subscriber = db.get(Subscriber, explicit_subscriber_id)
        reseller_id = subscriber.reseller_id if subscriber is not None else None
        return ContactResolution(
            status="explicit_subscriber" if subscriber is not None else "unmatched",
            normalized_contact=normalized,
            subscriber_id=subscriber.id if subscriber is not None else None,
            reseller_id=reseller_id,
            matched_subscriber_ids=[str(subscriber.id)]
            if subscriber is not None
            else [],
            suppressed_subscriber_ids=[],
            matched_reseller_ids=[str(reseller_id)] if reseller_id is not None else [],
        )

    active_link = None
    if normalized:
        active_link = (
            db.query(InboxContactLink)
            .filter(InboxContactLink.channel_type == channel_type)
            .filter(InboxContactLink.normalized_contact == normalized)
            .filter(InboxContactLink.is_active.is_(True))
            .first()
        )
    if active_link is not None:
        subscriber = (
            db.get(Subscriber, active_link.subscriber_id)
            if active_link.subscriber_id is not None
            else None
        )
        reseller = (
            db.get(Reseller, active_link.reseller_id)
            if active_link.reseller_id is not None
            else None
        )
        if subscriber is not None and _subscriber_is_linkable(subscriber):
            return ContactResolution(
                status="linked_subscriber",
                normalized_contact=normalized,
                subscriber_id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                matched_subscriber_ids=[str(subscriber.id)],
                suppressed_subscriber_ids=[],
                matched_reseller_ids=[str(subscriber.reseller_id)]
                if subscriber.reseller_id is not None
                else [],
            )
        if subscriber is not None:
            return ContactResolution(
                status="suppressed_inactive",
                normalized_contact=normalized,
                subscriber_id=None,
                reseller_id=None,
                matched_subscriber_ids=[],
                suppressed_subscriber_ids=[str(subscriber.id)],
                matched_reseller_ids=[],
            )
        if reseller is not None and reseller.is_active:
            return ContactResolution(
                status="linked_reseller",
                normalized_contact=normalized,
                subscriber_id=None,
                reseller_id=reseller.id,
                matched_subscriber_ids=[],
                suppressed_subscriber_ids=[],
                matched_reseller_ids=[str(reseller.id)],
            )

    matched_subscribers: list[Subscriber] = []
    suppressed_subscribers: list[Subscriber] = []
    if normalized:
        for subscriber in db.query(Subscriber).all():
            candidate = _normalize_contact_with_country(
                channel_type,
                _subscriber_contact(subscriber, channel_type),
                country_code=country_code,
            )
            if candidate != normalized:
                continue
            if _subscriber_is_linkable(subscriber):
                matched_subscribers.append(subscriber)
            else:
                suppressed_subscribers.append(subscriber)

    matched_resellers: list[Reseller] = []
    if normalized:
        for reseller in db.query(Reseller).filter(Reseller.is_active.is_(True)).all():
            candidate = _normalize_contact_with_country(
                channel_type,
                _reseller_contact(reseller, channel_type),
                country_code=country_code,
            )
            if candidate == normalized:
                matched_resellers.append(reseller)

    selected_subscriber = (
        matched_subscribers[0] if len(matched_subscribers) == 1 else None
    )
    selected_reseller_id = None
    if selected_subscriber is not None:
        selected_reseller_id = selected_subscriber.reseller_id
    elif len(matched_resellers) == 1:
        selected_reseller_id = matched_resellers[0].id

    if selected_subscriber is not None:
        status = "linked_subscriber"
    elif selected_reseller_id is not None:
        status = "linked_reseller"
    elif matched_subscribers or matched_resellers:
        status = "ambiguous"
    elif suppressed_subscribers:
        status = "suppressed_inactive"
    else:
        status = "unmatched"

    return ContactResolution(
        status=status,
        normalized_contact=normalized,
        subscriber_id=selected_subscriber.id if selected_subscriber else None,
        reseller_id=selected_reseller_id,
        matched_subscriber_ids=[
            str(subscriber.id) for subscriber in matched_subscribers
        ],
        suppressed_subscriber_ids=[
            str(subscriber.id) for subscriber in suppressed_subscribers
        ],
        matched_reseller_ids=[str(reseller.id) for reseller in matched_resellers],
    )


def _find_duplicate_message(
    db: Session,
    *,
    channel_type: str,
    external_message_id: str | None,
) -> InboxMessage | None:
    if not external_message_id:
        return None
    return (
        db.query(InboxMessage)
        .filter(InboxMessage.channel_type == channel_type)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(InboxMessage.external_message_id == external_message_id)
        .first()
    )


def _find_open_conversation(
    db: Session,
    *,
    channel_type: str,
    external_thread_id: str,
) -> InboxConversation | None:
    return (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == channel_type)
        .filter(InboxConversation.external_thread_id == external_thread_id)
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.last_message_at.desc().nullslast())
        .first()
    )


def _thread_id(channel_type: str, normalized_contact: str | None, fallback: str) -> str:
    contact = normalized_contact or fallback.strip()
    return f"{channel_type}:{contact}"[:255]


def _message_body(value: object) -> str:
    if isinstance(value, dict):
        body = value.get("body") or value.get("text")
        return str(body or "").strip()
    return str(value or "").strip()


def receive_inbound_channel(
    db: Session,
    payload: InboundChannelPayload,
) -> InboundChannelReceiveResult:
    channel_type = str(payload.channel_type or "").strip()
    if channel_type not in {item.value for item in InboxChannelType}:
        raise ValueError("Unsupported inbox channel_type")
    body = _message_body(payload.body)
    if not body:
        raise ValueError("Inbound message body is required")

    duplicate = _find_duplicate_message(
        db,
        channel_type=channel_type,
        external_message_id=payload.external_message_id,
    )
    if duplicate is not None:
        return InboundChannelReceiveResult(
            kind="duplicate",
            conversation_id=str(duplicate.conversation_id),
            message_id=str(duplicate.id),
            duplicate=True,
        )

    resolution = resolve_contact_context(
        db,
        channel_type=channel_type,
        contact_address=payload.contact_address,
        subscriber_id=payload.subscriber_id,
    )
    external_thread_id = payload.external_thread_id or _thread_id(
        channel_type, resolution.normalized_contact, payload.contact_address
    )
    received_at = payload.received_at or datetime.now(UTC)
    conversation = _find_open_conversation(
        db,
        channel_type=channel_type,
        external_thread_id=external_thread_id,
    )
    if conversation is None:
        conversation = InboxConversation(
            subscriber_id=resolution.subscriber_id,
            channel_type=channel_type,
            status=InboxConversationStatus.open.value,
            subject=payload.subject or payload.contact_name,
            contact_address=resolution.normalized_contact or payload.contact_address,
            external_thread_id=external_thread_id,
            first_message_at=received_at,
            last_message_at=received_at,
            metadata_={"contact_resolution": resolution.as_metadata()},
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.last_message_at = received_at
        if resolution.subscriber_id and not conversation.subscriber_id:
            conversation.subscriber_id = resolution.subscriber_id
        metadata = dict(conversation.metadata_ or {})
        metadata["contact_resolution"] = resolution.as_metadata()
        conversation.metadata_ = metadata

    routing_plan = team_inbox_routing.build_email_team_routing_plan(
        db,
        to_addresses=[],
        cc_addresses=[],
        fallback_service_team_id=payload.fallback_service_team_id,
    )
    team_inbox_routing.apply_email_routing_plan(
        db,
        conversation=conversation,
        plan=routing_plan,
    )

    metadata = dict(payload.metadata or {})
    metadata["contact_resolution"] = resolution.as_metadata()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel_type,
        direction=InboxMessageDirection.inbound.value,
        subject=payload.subject,
        body=body,
        external_message_id=payload.external_message_id,
        external_thread_id=external_thread_id,
        from_address=resolution.normalized_contact or payload.contact_address,
        received_at=received_at,
        metadata_=metadata,
    )
    db.add(message)
    db.flush()
    team_inbox_media.promote_message_attachments(
        db,
        message=message,
        provider=str(metadata.get("provider") or "") or None,
    )
    conversation.last_message_at = received_at
    return InboundChannelReceiveResult(
        kind="received",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        duplicate=False,
        subscriber_id=str(resolution.subscriber_id)
        if resolution.subscriber_id
        else None,
        reseller_id=str(resolution.reseller_id) if resolution.reseller_id else None,
        resolution_status=resolution.status,
    )


def receive_whatsapp_webhook(
    db: Session,
    *,
    provider: str,
    payload: dict,
    fallback_service_team_id: str | UUID | None = None,
) -> InboundChannelReceiveResult:
    normalized = whatsapp_connector.normalize_inbound_webhook(
        provider=provider,
        payload=payload,
    )
    return receive_inbound_channel(
        db,
        InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address=str(normalized.get("from") or ""),
            body=_message_body(normalized.get("text")),
            external_message_id=(
                str(normalized.get("external_id"))
                if normalized.get("external_id")
                else None
            ),
            fallback_service_team_id=fallback_service_team_id,
            metadata={
                "provider": normalized.get("provider"),
                "attachments": payload.get("attachments")
                if isinstance(payload.get("attachments"), list)
                else [],
                "raw": normalized.get("raw"),
            },
        ),
    )
