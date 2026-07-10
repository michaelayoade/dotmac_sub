from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Reseller, Subscriber
from app.models.team_inbox import InboxContactLink, InboxConversation
from app.services.common import coerce_uuid
from app.services.team_inbox_channel_receive import _normalize_contact


class ContactLinkError(ValueError):
    pass


@dataclass(frozen=True)
class ContactLinkResult:
    contact_link_id: UUID
    channel_type: str
    normalized_contact: str
    subscriber_id: UUID | None
    reseller_id: UUID | None
    previous_link_ids_deactivated: list[UUID]


def _target(
    db: Session,
    *,
    subscriber_id: str | UUID | None,
    reseller_id: str | UUID | None,
) -> tuple[Subscriber | None, Reseller | None]:
    subscriber_uuid = coerce_uuid(subscriber_id)
    reseller_uuid = coerce_uuid(reseller_id)
    if bool(subscriber_uuid) == bool(reseller_uuid):
        raise ContactLinkError("Provide exactly one of subscriber_id or reseller_id.")
    subscriber = db.get(Subscriber, subscriber_uuid) if subscriber_uuid else None
    reseller = db.get(Reseller, reseller_uuid) if reseller_uuid else None
    if subscriber_uuid and subscriber is None:
        raise ContactLinkError("Subscriber not found.")
    if reseller_uuid and reseller is None:
        raise ContactLinkError("Reseller not found.")
    if reseller is not None and not reseller.is_active:
        raise ContactLinkError("Cannot link an inactive reseller.")
    return subscriber, reseller


def link_conversation_contact(
    db: Session,
    *,
    conversation: InboxConversation,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    linked_by_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkResult:
    if not conversation.channel_type or not conversation.contact_address:
        raise ContactLinkError("Conversation does not have a linkable contact address.")
    subscriber, reseller = _target(
        db,
        subscriber_id=subscriber_id,
        reseller_id=reseller_id,
    )
    normalized_contact = _normalize_contact(
        db, conversation.channel_type, conversation.contact_address
    )
    if not normalized_contact:
        raise ContactLinkError("Conversation contact address cannot be normalized.")

    now = datetime.now(UTC)
    deactivated: list[UUID] = []
    for link in (
        db.query(InboxContactLink)
        .filter(InboxContactLink.channel_type == conversation.channel_type)
        .filter(InboxContactLink.normalized_contact == normalized_contact)
        .filter(InboxContactLink.is_active.is_(True))
        .all()
    ):
        link.is_active = False
        metadata = dict(link.metadata_ or {})
        metadata["deactivated_at"] = now.isoformat()
        metadata["deactivated_by_person_id"] = str(linked_by_person_id or "") or None
        metadata["deactivated_for_conversation_id"] = str(conversation.id)
        link.metadata_ = metadata
        deactivated.append(link.id)

    contact_link = InboxContactLink(
        channel_type=conversation.channel_type,
        normalized_contact=normalized_contact,
        subscriber_id=subscriber.id if subscriber is not None else None,
        reseller_id=reseller.id if reseller is not None else None,
        linked_by_person_id=coerce_uuid(linked_by_person_id),
        source="manual_inbox_conversation",
        is_active=True,
        metadata_={
            "conversation_id": str(conversation.id),
            "note": note,
        },
    )
    db.add(contact_link)
    db.flush()

    if subscriber is not None:
        conversation.subscriber_id = subscriber.id
    metadata = dict(conversation.metadata_ or {})
    contact_resolution = dict(metadata.get("contact_resolution") or {})
    linked_reseller_id = reseller.id if reseller is not None else None
    if subscriber is not None and subscriber.reseller_id is not None:
        linked_reseller_id = subscriber.reseller_id
    contact_resolution.update(
        {
            "status": "linked_subscriber" if subscriber else "linked_reseller",
            "normalized_contact": normalized_contact,
            "subscriber_id": str(subscriber.id) if subscriber else None,
            "reseller_id": str(linked_reseller_id) if linked_reseller_id else None,
            "manual_contact_link_id": str(contact_link.id),
        }
    )
    metadata["contact_resolution"] = contact_resolution
    metadata["manual_contact_link"] = {
        "id": str(contact_link.id),
        "linked_at": now.isoformat(),
        "linked_by_person_id": str(linked_by_person_id or "") or None,
        "note": note,
    }
    conversation.metadata_ = metadata

    return ContactLinkResult(
        contact_link_id=contact_link.id,
        channel_type=contact_link.channel_type,
        normalized_contact=contact_link.normalized_contact,
        subscriber_id=contact_link.subscriber_id,
        reseller_id=contact_link.reseller_id,
        previous_link_ids_deactivated=deactivated,
    )
