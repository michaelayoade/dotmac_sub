from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxMessage,
)


@dataclass(frozen=True)
class InboxTimelineTeam:
    service_team_id: str
    service_team_name: str | None
    service_team_type: str | None
    role: str
    source: str
    is_active: bool


@dataclass(frozen=True)
class InboxTimelineAssignment:
    person_id: str
    service_team_id: str
    service_team_name: str | None
    assigned_by_person_id: str | None
    assigned_at: datetime
    is_active: bool


@dataclass(frozen=True)
class InboxTimelineMessage:
    id: str
    channel_type: str
    direction: str
    subject: str | None
    body: str | None
    from_address: str | None
    to_addresses: list
    cc_addresses: list
    sent_at: datetime | None
    received_at: datetime | None
    created_at: datetime
    metadata: dict | None


@dataclass(frozen=True)
class InboxConversationTimeline:
    id: str
    subscriber_id: str | None
    primary_service_team_id: str | None
    channel_type: str
    status: str
    subject: str | None
    contact_address: str | None
    external_thread_id: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    metadata: dict | None
    teams: list[InboxTimelineTeam]
    assignments: list[InboxTimelineAssignment]
    messages: list[InboxTimelineMessage]


def _conversation_id(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def get_conversation_timeline(
    db: Session,
    conversation_id: str | UUID,
) -> InboxConversationTimeline | None:
    conversation = db.get(InboxConversation, _conversation_id(conversation_id))
    if conversation is None or not conversation.is_active:
        return None

    team_rows = (
        db.query(InboxConversationTeam, ServiceTeam)
        .outerjoin(ServiceTeam, ServiceTeam.id == InboxConversationTeam.service_team_id)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .order_by(
            InboxConversationTeam.role.asc(), InboxConversationTeam.created_at.asc()
        )
        .all()
    )
    assignment_rows = (
        db.query(InboxConversationAssignment, ServiceTeam)
        .outerjoin(
            ServiceTeam,
            ServiceTeam.id == InboxConversationAssignment.service_team_id,
        )
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .order_by(
            InboxConversationAssignment.is_active.desc(),
            InboxConversationAssignment.assigned_at.desc(),
        )
        .all()
    )
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(
            InboxMessage.created_at.asc(),
            InboxMessage.received_at.asc(),
            InboxMessage.sent_at.asc(),
        )
        .all()
    )

    return InboxConversationTimeline(
        id=str(conversation.id),
        subscriber_id=str(conversation.subscriber_id)
        if conversation.subscriber_id is not None
        else None,
        primary_service_team_id=str(conversation.primary_service_team_id)
        if conversation.primary_service_team_id is not None
        else None,
        channel_type=conversation.channel_type,
        status=conversation.status,
        subject=conversation.subject,
        contact_address=conversation.contact_address,
        external_thread_id=conversation.external_thread_id,
        first_message_at=conversation.first_message_at,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        metadata=conversation.metadata_,
        teams=[
            InboxTimelineTeam(
                service_team_id=str(link.service_team_id),
                service_team_name=team.name if team is not None else None,
                service_team_type=team.team_type if team is not None else None,
                role=link.role,
                source=link.source,
                is_active=link.is_active,
            )
            for link, team in team_rows
        ],
        assignments=[
            InboxTimelineAssignment(
                person_id=str(assignment.person_id),
                service_team_id=str(assignment.service_team_id),
                service_team_name=team.name if team is not None else None,
                assigned_by_person_id=str(assignment.assigned_by_person_id)
                if assignment.assigned_by_person_id is not None
                else None,
                assigned_at=assignment.assigned_at,
                is_active=assignment.is_active,
            )
            for assignment, team in assignment_rows
        ],
        messages=[
            InboxTimelineMessage(
                id=str(message.id),
                channel_type=message.channel_type,
                direction=message.direction,
                subject=message.subject,
                body=message.body,
                from_address=message.from_address,
                to_addresses=list(message.to_addresses or []),
                cc_addresses=list(message.cc_addresses or []),
                sent_at=message.sent_at,
                received_at=message.received_at,
                created_at=message.created_at,
                metadata=message.metadata_,
            )
            for message in messages
        ],
    )
