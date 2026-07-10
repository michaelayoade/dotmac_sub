from __future__ import annotations

from dataclasses import dataclass
from email.utils import getaddresses
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationTeam,
    InboxTeamRole,
    InboxTeamSource,
    TeamInboxEmailRoute,
)


@dataclass(frozen=True)
class EmailTeamRecipientMatch:
    service_team_id: str
    email_address: str
    recipient_kind: str
    is_primary_route: bool
    priority: int


@dataclass(frozen=True)
class EmailTeamRoutingPlan:
    primary_service_team_id: str | None
    participant_service_team_ids: list[str]
    matches: list[EmailTeamRecipientMatch]
    unmatched_recipients: list[str]


def normalize_email_address(value: str | None) -> str | None:
    if not value:
        return None
    parsed = getaddresses([value])
    address = parsed[0][1] if parsed else value
    normalized = address.strip().lower()
    return normalized or None


def normalize_email_addresses(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for _name, address in getaddresses([str(raw)]):
            value = normalize_email_address(address)
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
    return normalized


def _coerce_uuid(value: str | UUID | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def build_email_team_routing_plan(
    db: Session,
    *,
    to_addresses: list[str] | tuple[str, ...] | None = None,
    cc_addresses: list[str] | tuple[str, ...] | None = None,
    fallback_service_team_id: str | UUID | None = None,
) -> EmailTeamRoutingPlan:
    to_normalized = normalize_email_addresses(to_addresses)
    cc_normalized = normalize_email_addresses(cc_addresses)
    all_recipients = list(dict.fromkeys([*to_normalized, *cc_normalized]))
    fallback_team_id = _coerce_uuid(fallback_service_team_id)

    if not all_recipients:
        return EmailTeamRoutingPlan(
            primary_service_team_id=fallback_team_id,
            participant_service_team_ids=[fallback_team_id] if fallback_team_id else [],
            matches=[],
            unmatched_recipients=[],
        )

    routes = (
        db.query(TeamInboxEmailRoute)
        .filter(TeamInboxEmailRoute.is_active.is_(True))
        .filter(TeamInboxEmailRoute.email_address.in_(all_recipients))
        .all()
    )

    matched_addresses = {route.email_address for route in routes}
    matches = [
        EmailTeamRecipientMatch(
            service_team_id=str(route.service_team_id),
            email_address=route.email_address,
            recipient_kind="to" if route.email_address in to_normalized else "cc",
            is_primary_route=route.is_primary,
            priority=route.priority,
        )
        for route in routes
    ]
    matches.sort(
        key=lambda match: (
            0 if match.recipient_kind == "to" else 1,
            match.priority,
            0 if match.is_primary_route else 1,
            match.email_address,
        )
    )

    participant_ids: list[str] = []
    for match in matches:
        if match.service_team_id not in participant_ids:
            participant_ids.append(match.service_team_id)
    if fallback_team_id and fallback_team_id not in participant_ids:
        participant_ids.append(fallback_team_id)

    primary_team_id = matches[0].service_team_id if matches else fallback_team_id
    return EmailTeamRoutingPlan(
        primary_service_team_id=primary_team_id,
        participant_service_team_ids=participant_ids,
        matches=matches,
        unmatched_recipients=[
            address for address in all_recipients if address not in matched_addresses
        ],
    )


def apply_email_routing_plan(
    db: Session,
    *,
    conversation: InboxConversation,
    plan: EmailTeamRoutingPlan,
) -> InboxConversation:
    primary_team_id = _coerce_uuid(plan.primary_service_team_id)
    if primary_team_id:
        conversation.primary_service_team_id = UUID(primary_team_id)

    for team_id in plan.participant_service_team_ids:
        normalized_team_id = _coerce_uuid(team_id)
        if not normalized_team_id:
            continue
        team_uuid = UUID(normalized_team_id)
        role = (
            InboxTeamRole.owner.value
            if normalized_team_id == primary_team_id
            else InboxTeamRole.participant.value
        )
        match = next(
            (
                item
                for item in plan.matches
                if item.service_team_id == normalized_team_id
            ),
            None,
        )
        source = (
            InboxTeamSource.recipient_to.value
            if match and match.recipient_kind == "to"
            else InboxTeamSource.recipient_cc.value
            if match and match.recipient_kind == "cc"
            else InboxTeamSource.routing_rule.value
        )
        link = (
            db.query(InboxConversationTeam)
            .filter(InboxConversationTeam.conversation_id == conversation.id)
            .filter(InboxConversationTeam.service_team_id == team_uuid)
            .first()
        )
        if link is None:
            db.add(
                InboxConversationTeam(
                    conversation_id=conversation.id,
                    service_team_id=team_uuid,
                    role=role,
                    source=source,
                    is_active=True,
                )
            )
            continue
        link.role = role
        link.source = source
        link.is_active = True
    db.flush()
    return conversation
