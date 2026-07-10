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


def _record_escalation_metadata(
    conversation: InboxConversation,
    *,
    service_team_id: UUID,
    assigned_person_id: UUID | None,
    assigned_by_person_id: UUID | None,
    reason: str | None,
    kind: str,
    now: datetime,
) -> None:
    metadata = dict(conversation.metadata_ or {})
    metadata["last_inbox_escalation"] = {
        "service_team_id": str(service_team_id),
        "assigned_person_id": str(assigned_person_id) if assigned_person_id else None,
        "assigned_by_person_id": (
            str(assigned_by_person_id) if assigned_by_person_id else None
        ),
        "reason": reason,
        "kind": kind,
        "at": now.isoformat(),
    }
    conversation.metadata_ = metadata


def assign_conversation_to_agent(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    person_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    person_uuid = _coerce_uuid(person_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    assigned_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )
    if person_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_agent",
            service_team_id=str(team_uuid),
            reason="person_id must be a valid UUID",
        )

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=str(team_uuid),
            reason="service_team_id must reference an active team",
        )

    member = (
        db.query(ServiceTeamMember)
        .filter(ServiceTeamMember.team_id == team_uuid)
        .filter(ServiceTeamMember.person_id == person_uuid)
        .filter(ServiceTeamMember.is_active.is_(True))
        .one_or_none()
    )
    if member is None:
        return InboxAssignmentResult(
            kind="invalid_agent",
            service_team_id=str(team_uuid),
            reason="person_id must be an active member of the target team",
        )

    set_conversation_owner_team(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        source=source,
    )
    for assignment in conversation.assignments:
        if assignment.is_active:
            assignment.is_active = False

    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team_uuid,
        person_id=person_uuid,
        assigned_by_person_id=actor_uuid,
        assigned_at=assigned_at,
        is_active=True,
        metadata_={"reason": reason, "source": source},
    )
    db.add(assignment)
    _record_escalation_metadata(
        conversation,
        service_team_id=team_uuid,
        assigned_person_id=person_uuid,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        kind="assigned",
        now=assigned_at,
    )
    db.flush()
    return InboxAssignmentResult(
        kind="assigned",
        service_team_id=str(team_uuid),
        assigned_person_id=str(person_uuid),
    )


def queue_conversation_for_team(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    queued_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=str(team_uuid),
            reason="service_team_id must reference an active team",
        )

    set_conversation_owner_team(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        source=source,
    )
    for assignment in conversation.assignments:
        if assignment.is_active:
            assignment.is_active = False
    _record_escalation_metadata(
        conversation,
        service_team_id=team_uuid,
        assigned_person_id=None,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        kind="queued",
        now=queued_at,
    )
    db.flush()
    return InboxAssignmentResult(
        kind="queued",
        service_team_id=str(team_uuid),
        reason="manual_queue",
    )


def assign_conversation_to_available_agent(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    assigned_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )

    candidates = list_available_team_agents(db, team_uuid)
    if not candidates:
        team = db.get(ServiceTeam, team_uuid)
        if team is None or not team.is_active:
            return InboxAssignmentResult(
                kind="invalid_team",
                service_team_id=str(team_uuid),
                reason="service_team_id must reference an active team",
            )
        set_conversation_owner_team(
            db,
            conversation=conversation,
            service_team_id=team_uuid,
            source=source,
        )
        _record_escalation_metadata(
            conversation,
            service_team_id=team_uuid,
            assigned_person_id=None,
            assigned_by_person_id=actor_uuid,
            reason=reason,
            kind="queued",
            now=assigned_at,
        )
        db.flush()
        return InboxAssignmentResult(
            kind="queued",
            service_team_id=str(team_uuid),
            reason="no_available_agent",
        )

    selected = candidates[0]
    return assign_conversation_to_agent(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        person_id=selected.person_id,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        source=source,
        now=assigned_at,
    )
