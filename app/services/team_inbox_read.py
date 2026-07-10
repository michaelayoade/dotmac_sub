from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
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


@dataclass(frozen=True)
class InboxConversationListRow:
    id: str
    subscriber_id: str | None
    primary_service_team_id: str | None
    primary_service_team_name: str | None
    primary_service_team_type: str | None
    channel_type: str
    status: str
    subject: str | None
    contact_address: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    latest_message_direction: str | None
    latest_message_body: str | None
    latest_message_at: datetime | None
    active_assigned_person_id: str | None
    needs_response: bool
    team_count: int


@dataclass(frozen=True)
class InboxConversationListResult:
    items: list[InboxConversationListRow]
    count: int
    limit: int
    offset: int


def _conversation_id(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _optional_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or str(value).strip() == "":
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _message_time(message: InboxMessage) -> datetime:
    return message.received_at or message.sent_at or message.created_at


def _latest_messages_by_conversation(
    db: Session,
    conversation_ids: list[UUID],
) -> dict[UUID, InboxMessage]:
    if not conversation_ids:
        return {}
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    latest: dict[UUID, InboxMessage] = {}
    for message in messages:
        if message.direction == InboxMessageDirection.internal.value:
            continue
        current = latest.get(message.conversation_id)
        if current is None or _message_time(message) >= _message_time(current):
            latest[message.conversation_id] = message
    return latest


def list_conversations(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    channel_type: str | None = None,
    service_team_id: str | UUID | None = None,
    assigned_person_id: str | UUID | None = None,
    needs_response: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> InboxConversationListResult:
    query = (
        db.query(InboxConversation, ServiceTeam)
        .outerjoin(
            ServiceTeam, ServiceTeam.id == InboxConversation.primary_service_team_id
        )
        .filter(InboxConversation.is_active.is_(True))
    )
    clean_search = (search or "").strip()
    if clean_search:
        like = f"%{clean_search}%"
        query = query.filter(
            or_(
                InboxConversation.subject.ilike(like),
                InboxConversation.contact_address.ilike(like),
                InboxConversation.external_thread_id.ilike(like),
            )
        )
    if status:
        query = query.filter(InboxConversation.status == status)
    if channel_type:
        query = query.filter(InboxConversation.channel_type == channel_type)

    team_uuid = _optional_uuid(service_team_id)
    if team_uuid is not None:
        query = query.join(
            InboxConversationTeam,
            InboxConversationTeam.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversationTeam.service_team_id == team_uuid,
            InboxConversationTeam.is_active.is_(True),
        )

    assignee_uuid = _optional_uuid(assigned_person_id)
    if assignee_uuid is not None:
        query = query.join(
            InboxConversationAssignment,
            InboxConversationAssignment.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversationAssignment.person_id == assignee_uuid,
            InboxConversationAssignment.is_active.is_(True),
        )

    ordered_query = query.order_by(
        InboxConversation.last_message_at.desc().nullslast(),
        InboxConversation.created_at.desc(),
    )
    total = query.count()
    rows = (
        ordered_query.all()
        if needs_response
        else ordered_query.limit(limit).offset(offset).all()
    )
    conversations = [conversation for conversation, _team in rows]
    conversation_ids = [conversation.id for conversation in conversations]
    latest_messages = _latest_messages_by_conversation(db, conversation_ids)
    active_assignments = (
        {
            assignment.conversation_id: assignment
            for assignment in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        if conversation_ids
        else {}
    )
    team_counts = (
        {
            conversation_id: count
            for conversation_id, count in db.query(
                InboxConversationTeam.conversation_id,
                func.count(InboxConversationTeam.id),
            )
            .filter(InboxConversationTeam.conversation_id.in_(conversation_ids))
            .filter(InboxConversationTeam.is_active.is_(True))
            .group_by(InboxConversationTeam.conversation_id)
            .all()
        }
        if conversation_ids
        else {}
    )

    items: list[InboxConversationListRow] = []
    for conversation, team in rows:
        latest = latest_messages.get(conversation.id)
        active_assignment = active_assignments.get(conversation.id)
        row_needs_response = (
            latest is not None
            and latest.direction == InboxMessageDirection.inbound.value
            and conversation.status != "resolved"
        )
        if needs_response and not row_needs_response:
            continue
        items.append(
            InboxConversationListRow(
                id=str(conversation.id),
                subscriber_id=str(conversation.subscriber_id)
                if conversation.subscriber_id is not None
                else None,
                primary_service_team_id=str(conversation.primary_service_team_id)
                if conversation.primary_service_team_id is not None
                else None,
                primary_service_team_name=team.name if team is not None else None,
                primary_service_team_type=team.team_type if team is not None else None,
                channel_type=conversation.channel_type,
                status=conversation.status,
                subject=conversation.subject,
                contact_address=conversation.contact_address,
                first_message_at=conversation.first_message_at,
                last_message_at=conversation.last_message_at,
                latest_message_direction=latest.direction
                if latest is not None
                else None,
                latest_message_body=latest.body if latest is not None else None,
                latest_message_at=_message_time(latest) if latest is not None else None,
                active_assigned_person_id=str(active_assignment.person_id)
                if active_assignment is not None
                else None,
                needs_response=row_needs_response,
                team_count=int(team_counts.get(conversation.id, 0)),
            )
        )
    filtered_count = len(items) if needs_response else total
    page_items = items[offset : offset + limit] if needs_response else items
    return InboxConversationListResult(
        items=page_items,
        count=filtered_count,
        limit=limit,
        offset=offset,
    )


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
