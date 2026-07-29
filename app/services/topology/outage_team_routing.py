"""Canonical configuration owner for exact outage-to-team routes."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.operational_escalation import (
    OutageTeamRoutingPolicy,
    OutageTeamRoutingPurpose,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityKey,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "network.outage_team_routing"
CONCERN = "outage team routing policy"

_SET_ROUTE = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="set_outage_team_route",
)


class OutageTeamRoutingError(DomainError):
    pass


@dataclass(frozen=True)
class SetOutageTeamRoute:
    context: CommandContext
    policy_id: UUID
    purpose: str
    service_team_id: UUID
    priority: int
    is_active: bool


@dataclass(frozen=True)
class OutageTeamRouteMutation:
    policy_id: UUID
    service_team_id: UUID
    purpose: str
    is_active: bool
    replayed: bool


def _error(code: str, message: str, **details: object) -> OutageTeamRoutingError:
    return OutageTeamRoutingError(code=code, message=message, details=details)


def _required_capability(purpose: str) -> ServiceTeamCapabilityKey:
    if purpose == OutageTeamRoutingPurpose.primary_owner:
        return ServiceTeamCapabilityKey.network_outages_coordinate
    if purpose in {
        OutageTeamRoutingPurpose.lead_watcher,
        OutageTeamRoutingPurpose.watcher,
    }:
        return ServiceTeamCapabilityKey.network_outages_observe
    raise _error(
        "outage_team_route_invalid",
        "The outage routing purpose is not registered.",
        purpose=purpose,
    )


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    actor_type_value, separator, actor_id = str(context.actor or "").partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError:
        return AuditActorType.system, None
    return actor_type, actor_id if separator and actor_id else None


def set_outage_team_route(
    db: Session,
    command: SetOutageTeamRoute,
) -> OutageTeamRouteMutation:
    required_capability = _required_capability(command.purpose)
    priority = int(command.priority)
    if priority < 0 or priority > 10000:
        raise _error(
            "outage_team_route_invalid",
            "Route priority must be between 0 and 10000.",
        )

    def apply() -> OutageTeamRouteMutation:
        team = db.scalar(
            select(ServiceTeam)
            .where(ServiceTeam.id == command.service_team_id)
            .with_for_update()
        )
        if team is None or not team.is_active:
            raise _error(
                "outage_team_route_team_invalid",
                "The selected outage team is missing or inactive.",
                service_team_id=str(command.service_team_id),
            )
        capability = db.scalar(
            select(ServiceTeamCapability)
            .where(
                ServiceTeamCapability.team_id == team.id,
                ServiceTeamCapability.capability_key == required_capability.value,
                ServiceTeamCapability.is_active.is_(True),
            )
            .with_for_update()
        )
        if capability is None:
            raise _error(
                "outage_team_route_capability_missing",
                "The selected team lacks the required outage capability.",
                service_team_id=str(team.id),
                capability_key=required_capability.value,
            )
        policy = db.scalar(
            select(OutageTeamRoutingPolicy)
            .where(OutageTeamRoutingPolicy.id == command.policy_id)
            .with_for_update()
        )
        if (
            command.is_active
            and command.purpose == OutageTeamRoutingPurpose.primary_owner
        ):
            conflicting = db.scalar(
                select(OutageTeamRoutingPolicy.id)
                .where(
                    OutageTeamRoutingPolicy.id != command.policy_id,
                    OutageTeamRoutingPolicy.purpose
                    == OutageTeamRoutingPurpose.primary_owner,
                    OutageTeamRoutingPolicy.is_active.is_(True),
                )
                .with_for_update()
            )
            if conflicting is not None:
                raise _error(
                    "outage_team_route_primary_conflict",
                    "Deactivate the current primary outage route first.",
                    conflicting_policy_id=str(conflicting),
                )
        desired = (
            command.purpose,
            team.id,
            required_capability.value,
            priority,
            command.is_active,
        )
        if policy is not None:
            current = (
                policy.purpose,
                policy.service_team_id,
                policy.required_capability_key,
                policy.priority,
                policy.is_active,
            )
            if current == desired:
                return OutageTeamRouteMutation(
                    policy_id=policy.id,
                    service_team_id=team.id,
                    purpose=policy.purpose,
                    is_active=policy.is_active,
                    replayed=True,
                )
            if policy.purpose != command.purpose or policy.service_team_id != team.id:
                raise _error(
                    "outage_team_route_identity_collision",
                    "The route identifier is already bound to another route.",
                    policy_id=str(policy.id),
                )
            policy.required_capability_key = required_capability.value
            policy.priority = priority
            policy.is_active = command.is_active
        else:
            policy = OutageTeamRoutingPolicy(
                id=command.policy_id,
                purpose=command.purpose,
                service_team_id=team.id,
                required_capability_key=required_capability.value,
                priority=priority,
                is_active=command.is_active,
            )
            db.add(policy)
        db.flush()
        actor_type, actor_id = _actor(command.context)
        evidence = {
            "schema_version": 1,
            "owner": OWNER,
            "policy_id": str(policy.id),
            "service_team_id": str(team.id),
            "purpose": policy.purpose,
            "required_capability_key": policy.required_capability_key,
            "priority": policy.priority,
            "is_active": policy.is_active,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        }
        stage_audit_event(
            db,
            action="outage.team_route_changed",
            entity_type="outage_team_routing_policy",
            entity_id=str(policy.id),
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=evidence,
        )
        emit_event(
            db,
            EventType.outage_team_route_changed,
            {
                **evidence,
                "aggregate_type": "outage_team_routing_policy",
                "aggregate_id": str(policy.id),
                "aggregate_version": str(command.context.command_id),
            },
            actor=command.context.actor,
        )
        return OutageTeamRouteMutation(
            policy_id=policy.id,
            service_team_id=team.id,
            purpose=policy.purpose,
            is_active=policy.is_active,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_SET_ROUTE,
        context=command.context,
        operation=apply,
    )
