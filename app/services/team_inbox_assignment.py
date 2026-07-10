from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxTeamRole,
    InboxTeamSource,
)

DEFAULT_MAX_CONCURRENT_CONVERSATIONS = 3


@dataclass(frozen=True)
class InboxAgentCandidate:
    person_id: str
    active_conversation_count: int
    max_concurrent_conversations: int


@dataclass(frozen=True)
class InboxAssignmentResult:
    kind: str
    service_team_id: str | None
    assigned_person_id: str | None = None
    reason: str | None = None


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _effective_presence_status(presence: InboxAgentPresence) -> str:
    return (
        presence.manual_override_status
        or presence.status
        or InboxAgentPresenceStatus.offline.value
    )


def list_available_team_agents(
    db: Session,
    service_team_id: str | UUID,
    *,
    default_max_concurrent: int = DEFAULT_MAX_CONCURRENT_CONVERSATIONS,
) -> list[InboxAgentCandidate]:
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        return []

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return []

    members = (
        db.query(ServiceTeamMember)
        .filter(ServiceTeamMember.team_id == team_uuid)
        .filter(ServiceTeamMember.is_active.is_(True))
        .all()
    )
    if not members:
        return []

    person_ids = [member.person_id for member in members]
    presences = {
        row.person_id: row
        for row in db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id.in_(person_ids))
        .all()
    }
    active_count_rows = (
        db.query(
            InboxConversationAssignment.person_id,
            func.count(InboxConversationAssignment.id),
        )
        .filter(InboxConversationAssignment.is_active.is_(True))
        .filter(InboxConversationAssignment.person_id.in_(person_ids))
        .group_by(InboxConversationAssignment.person_id)
        .all()
    )
    active_counts = {
        person_id: int(assignment_count)
        for person_id, assignment_count in active_count_rows
    }

    candidates: list[InboxAgentCandidate] = []
    for member in members:
        presence = presences.get(member.person_id)
        if presence is None:
            continue
        if (
            _effective_presence_status(presence)
            != InboxAgentPresenceStatus.online.value
        ):
            continue
        active_count = active_counts.get(member.person_id, 0)
        max_concurrent = (
            presence.max_concurrent_conversations
            or default_max_concurrent
            or DEFAULT_MAX_CONCURRENT_CONVERSATIONS
        )
        if active_count >= max_concurrent:
            continue
        candidates.append(
            InboxAgentCandidate(
                person_id=str(member.person_id),
                active_conversation_count=active_count,
                max_concurrent_conversations=max_concurrent,
            )
        )

    candidates.sort(key=lambda item: (item.active_conversation_count, item.person_id))
    return candidates


def set_conversation_owner_team(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    source: str = InboxTeamSource.escalation.value,
) -> InboxConversation:
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        raise ValueError("service_team_id must be a valid UUID")

    conversation.primary_service_team_id = team_uuid
    for link in conversation.team_links:
        if link.service_team_id == team_uuid:
            link.role = InboxTeamRole.owner.value
            link.source = source
            link.is_active = True
        elif link.role == InboxTeamRole.owner.value:
            link.role = InboxTeamRole.participant.value

    if not any(link.service_team_id == team_uuid for link in conversation.team_links):
        db.add(
            InboxConversationTeam(
                conversation_id=conversation.id,
                service_team_id=team_uuid,
                role=InboxTeamRole.owner.value,
                source=source,
                is_active=True,
            )
        )
    db.flush()
    return conversation


def assign_conversation_to_available_agent(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    now: datetime | None = None,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )

    set_conversation_owner_team(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        source=InboxTeamSource.escalation.value,
    )
    candidates = list_available_team_agents(db, team_uuid)
    if not candidates:
        return InboxAssignmentResult(
            kind="queued",
            service_team_id=str(team_uuid),
            reason="no_available_agent",
        )

    for assignment in conversation.assignments:
        if assignment.is_active:
            assignment.is_active = False

    selected = candidates[0]
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team_uuid,
        person_id=UUID(selected.person_id),
        assigned_by_person_id=_coerce_uuid(assigned_by_person_id),
        assigned_at=now or datetime.now(UTC),
        is_active=True,
    )
    db.add(assignment)
    db.flush()
    return InboxAssignmentResult(
        kind="assigned",
        service_team_id=str(team_uuid),
        assigned_person_id=selected.person_id,
    )
