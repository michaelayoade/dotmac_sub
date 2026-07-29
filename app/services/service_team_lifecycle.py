"""Authoritative native service-team lifecycle and administration projections.

Ticket configuration, Inbox, workqueue, outage, project, and field-work callers
consume service teams. They do not own team identity or membership lifecycle.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.gis import GeoArea
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamExternalReference,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamMemberRole,
    ServiceTeamRelationship,
    ServiceTeamRelationshipType,
    ServiceTeamResponsibilityDefinition,
    ServiceTeamResponsibilityKey,
    ServiceTeamScopeBinding,
    ServiceTeamScopeType,
)
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "operations.service_team_lifecycle"
LIFECYCLE_CONCERN = "service-team lifecycle"
MEMBERSHIP_CONCERN = "service-team membership lifecycle"
RELATIONSHIP_CONCERN = "service-team relationship lifecycle"
EXTERNAL_REFERENCE_CONCERN = "service-team external reference observations"
LEGACY_SHADOW_VERIFICATION_CONCERN = "service-team legacy-shadow verification"

_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="create_service_team",
)
_UPDATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="update_service_team",
)
_SET_ACTIVE = OwnerCommandDefinition(
    owner=OWNER,
    concern=LIFECYCLE_CONCERN,
    name="set_service_team_active",
)
_ADD_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="add_service_team_member",
)
_UPDATE_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="update_service_team_member",
)
_REMOVE_MEMBER = OwnerCommandDefinition(
    owner=OWNER,
    concern=MEMBERSHIP_CONCERN,
    name="remove_service_team_member",
)
_SET_RELATIONSHIP = OwnerCommandDefinition(
    owner=OWNER,
    concern=RELATIONSHIP_CONCERN,
    name="set_service_team_relationship",
)
_OBSERVE_EXTERNAL_REFERENCE = OwnerCommandDefinition(
    owner=OWNER,
    concern=EXTERNAL_REFERENCE_CONCERN,
    name="observe_service_team_external_reference",
)


class ServiceTeamLifecycleError(DomainError):
    """Stable, transport-neutral service-team command rejection."""


@dataclass(frozen=True)
class CreateServiceTeam:
    context: CommandContext
    team_id: UUID
    name: str
    capability_keys: tuple[ServiceTeamCapabilityKey, ...]
    geo_area_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class UpdateServiceTeam:
    context: CommandContext
    team_id: UUID
    expected_updated_at: datetime
    name: str
    capability_keys: tuple[ServiceTeamCapabilityKey, ...]
    geo_area_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class SetServiceTeamActive:
    context: CommandContext
    team_id: UUID
    expected_updated_at: datetime
    is_active: bool
    reason: str


@dataclass(frozen=True)
class AddServiceTeamMember:
    context: CommandContext
    team_id: UUID
    system_user_id: UUID
    responsibility_keys: tuple[ServiceTeamResponsibilityKey, ...]


@dataclass(frozen=True)
class SetServiceTeamMemberResponsibilities:
    context: CommandContext
    team_id: UUID
    member_id: UUID
    responsibility_keys: tuple[ServiceTeamResponsibilityKey, ...]


@dataclass(frozen=True)
class RemoveServiceTeamMember:
    context: CommandContext
    team_id: UUID
    member_id: UUID
    reason: str


@dataclass(frozen=True)
class SetServiceTeamRelationship:
    context: CommandContext
    relationship_id: UUID
    parent_team_id: UUID
    child_team_id: UUID
    relationship_type: ServiceTeamRelationshipType
    is_active: bool


@dataclass(frozen=True)
class ObserveServiceTeamExternalReference:
    context: CommandContext
    reference_id: UUID
    team_id: UUID
    system: str
    entity_type: str
    external_reference: str
    observed_at: datetime
    is_active: bool


@dataclass(frozen=True)
class ServiceTeamMutation:
    team_id: UUID
    member_id: UUID | None
    operation: str
    replayed: bool


@dataclass(frozen=True)
class ServiceTeamTopologyMutation:
    team_id: UUID
    record_id: UUID
    operation: str
    replayed: bool


@dataclass(frozen=True)
class StaffOption:
    system_user_id: UUID
    person_party_id: UUID
    label: str
    email: str


@dataclass(frozen=True)
class ServiceTeamMemberView:
    member_id: UUID
    person_id: UUID
    system_user_id: UUID | None
    person_label: str
    person_email: str
    responsibilities: tuple[ServiceTeamResponsibilityKey, ...]
    is_active: bool
    staff_identity_active: bool
    created_at: datetime


class ServiceTeamLegacyShadowIssue(str, Enum):
    """Typed reason a retained legacy scalar cannot yet be retired."""

    team_type_capability_mismatch = "team_type_capability_mismatch"
    region_requires_geo_area_review = "region_requires_geo_area_review"
    manager_requires_explicit_composition = "manager_requires_explicit_composition"
    member_role_responsibility_mismatch = "member_role_responsibility_mismatch"

    @property
    def operator_message(self) -> str:
        return {
            self.team_type_capability_mismatch: (
                "Legacy team type does not match the active capability set."
            ),
            self.region_requires_geo_area_review: (
                "Legacy region text requires explicit GeoArea review."
            ),
            self.manager_requires_explicit_composition: (
                "Legacy manager requires explicit accountable-manager composition."
            ),
            self.member_role_responsibility_mismatch: (
                "Legacy member role does not match an active responsibility."
            ),
        }[self]


@dataclass(frozen=True)
class ServiceTeamView:
    team_id: UUID
    name: str
    capabilities: tuple[ServiceTeamCapabilityKey, ...]
    geo_areas: tuple[tuple[UUID, str], ...]
    accountable_manager_labels: tuple[str, ...]
    legacy_shadow_issues: tuple[ServiceTeamLegacyShadowIssue, ...]
    is_active: bool
    active_member_count: int
    created_at: datetime
    updated_at: datetime

    @property
    def legacy_shadow_drift(self) -> bool:
        return bool(self.legacy_shadow_issues)


@dataclass(frozen=True)
class ServiceTeamLegacyShadowAudit:
    team_count: int
    drift_team_count: int
    issue_counts: tuple[tuple[ServiceTeamLegacyShadowIssue, int], ...]

    @property
    def ready(self) -> bool:
        return self.drift_team_count == 0


@dataclass(frozen=True)
class ServiceTeamDetail:
    team: ServiceTeamView
    members: tuple[ServiceTeamMemberView, ...]
    available_staff: tuple[StaffOption, ...]
    actions: ServiceTeamActionEligibility


@dataclass(frozen=True)
class ServiceTeamActionEligibility:
    can_edit: bool
    can_add_member: bool
    can_activate: bool
    can_deactivate: bool
    lifecycle_block_reason: str | None


class ServiceTeamResolutionKind(str, Enum):
    identity_unavailable = "identity_unavailable"
    no_membership = "no_membership"
    resolved = "resolved"


@dataclass(frozen=True)
class ServiceTeamResolution:
    system_user_id: UUID
    person_party_id: UUID | None
    kind: ServiceTeamResolutionKind
    team_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class StaffServiceTeamScope:
    """Active native team scope for one authenticated staff principal."""

    system_user_id: UUID
    person_party_id: UUID | None
    identity_available: bool
    member_team_ids: tuple[UUID, ...]
    queue_lead_team_ids: tuple[UUID, ...]
    accountable_manager_team_ids: tuple[UUID, ...]

    @property
    def accessible_team_ids(self) -> tuple[UUID, ...]:
        return self.member_team_ids

    @property
    def coordinates_queue(self) -> bool:
        return bool(self.queue_lead_team_ids or self.accountable_manager_team_ids)

    @property
    def queue_scope_team_ids(self) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                {
                    *self.queue_lead_team_ids,
                    *self.accountable_manager_team_ids,
                },
                key=str,
            )
        )


@dataclass(frozen=True)
class ServiceTeamRoleRegionMember:
    person_id: UUID
    system_user_id: UUID
    label: str
    email: str
    team_names: tuple[str, ...]


@dataclass(frozen=True)
class ServiceTeamResponsibilityGroup:
    group_key: str
    responsibility: ServiceTeamResponsibilityKey
    members: tuple[ServiceTeamRoleRegionMember, ...]


@dataclass
class _ResponsibilityAccumulator:
    responsibility: ServiceTeamResponsibilityKey
    members: dict[UUID, tuple[SystemUser, set[str]]]


@dataclass(frozen=True)
class ServiceTeamList:
    items: tuple[ServiceTeamView, ...]
    total: int
    active_count: int
    inactive_count: int
    search: str
    active_filter: bool | None
    offset: int
    limit: int


def _error(code: str, message: str, **details: object) -> ServiceTeamLifecycleError:
    return ServiceTeamLifecycleError(code=code, message=message, details=details)


def _clean_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise _error("service_team_invalid", "Team name is required.", field="name")
    if len(name) > 160:
        raise _error(
            "service_team_invalid",
            "Team name must be 160 characters or fewer.",
            field="name",
        )
    return name


def _capability_keys(
    values: tuple[ServiceTeamCapabilityKey, ...],
) -> tuple[ServiceTeamCapabilityKey, ...]:
    keys = tuple(sorted(set(values), key=lambda item: item.value))
    if not keys:
        raise _error(
            "service_team_capability_required",
            "Select at least one registered team capability.",
        )
    return keys


def _responsibility_keys(
    values: tuple[ServiceTeamResponsibilityKey, ...],
) -> tuple[ServiceTeamResponsibilityKey, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _geo_area_ids(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values), key=str))


def _validate_registered_capabilities(
    db: Session,
    keys: tuple[ServiceTeamCapabilityKey, ...],
) -> None:
    registered = set(
        db.scalars(
            select(ServiceTeamCapabilityDefinition.key).where(
                ServiceTeamCapabilityDefinition.key.in_(
                    tuple(item.value for item in keys)
                ),
                ServiceTeamCapabilityDefinition.is_active.is_(True),
            )
        ).all()
    )
    missing = {item.value for item in keys} - registered
    if missing:
        raise _error(
            "service_team_capability_unregistered",
            "One or more team capabilities are not registered and active.",
            capability_keys=tuple(sorted(missing)),
        )


def _validate_registered_responsibilities(
    db: Session,
    keys: tuple[ServiceTeamResponsibilityKey, ...],
) -> None:
    if not keys:
        return
    registered = set(
        db.scalars(
            select(ServiceTeamResponsibilityDefinition.key).where(
                ServiceTeamResponsibilityDefinition.key.in_(
                    tuple(item.value for item in keys)
                ),
                ServiceTeamResponsibilityDefinition.is_active.is_(True),
            )
        ).all()
    )
    missing = {item.value for item in keys} - registered
    if missing:
        raise _error(
            "service_team_responsibility_unregistered",
            "One or more member responsibilities are not registered and active.",
            responsibility_keys=tuple(sorted(missing)),
        )


def _validate_geo_areas(db: Session, geo_area_ids: tuple[UUID, ...]) -> None:
    if not geo_area_ids:
        return
    active_ids = set(
        db.scalars(
            select(GeoArea.id).where(
                GeoArea.id.in_(geo_area_ids),
                GeoArea.is_active.is_(True),
            )
        ).all()
    )
    missing = set(geo_area_ids) - active_ids
    if missing:
        raise _error(
            "service_team_geo_area_invalid",
            "One or more geographic scopes are missing or inactive.",
            geo_area_ids=tuple(str(item) for item in sorted(missing, key=str)),
        )


def _clean_reason(value: str) -> str:
    reason = " ".join(str(value or "").split())
    if not reason:
        raise _error(
            "service_team_reason_required",
            "A reason is required for this lifecycle change.",
        )
    if len(reason) > 500:
        raise _error(
            "service_team_invalid",
            "Reason must be 500 characters or fewer.",
            field="reason",
        )
    return reason


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    raw_actor = str(context.actor or "")
    candidate = raw_actor.rsplit(":", maxsplit=1)[-1]
    try:
        UUID(candidate)
    except (TypeError, ValueError):
        return AuditActorType.system, None
    return AuditActorType.user, candidate


def _staff_label(user: SystemUser) -> str:
    return (
        str(user.display_name or "").strip()
        or f"{user.first_name} {user.last_name}".strip()
        or user.email
    )


def _staff_option(user: SystemUser) -> StaffOption:
    if user.person_party_id is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The selected staff member has no reviewed Person Party binding.",
            system_user_id=str(user.id),
        )
    return StaffOption(
        system_user_id=user.id,
        person_party_id=user.person_party_id,
        label=_staff_label(user),
        email=user.email,
    )


def _active_staff_identity(
    db: Session,
    system_user_id: UUID,
) -> tuple[SystemUser, UUID]:
    user = db.scalar(
        select(SystemUser).where(SystemUser.id == system_user_id).with_for_update()
    )
    if user is None or not user.is_active:
        raise _error(
            "service_team_staff_not_found",
            "The selected staff member is not active.",
            system_user_id=str(system_user_id),
        )
    if user.person_party_id is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The selected staff member has no reviewed Person Party binding.",
            system_user_id=str(system_user_id),
        )
    person = db.scalar(
        select(Party).where(Party.id == user.person_party_id).with_for_update()
    )
    if (
        person is None
        or person.party_type != PartyType.person.value
        or person.status != PartyIdentityStatus.active.value
    ):
        raise _error(
            "service_team_staff_identity_invalid",
            "The selected staff member's Person Party binding is not active.",
            system_user_id=str(system_user_id),
            person_party_id=str(user.person_party_id),
        )
    return user, user.person_party_id


def _active_staff_for_person_party(
    db: Session,
    person_party_id: UUID,
) -> SystemUser:
    user = db.scalar(
        select(SystemUser)
        .where(SystemUser.person_party_id == person_party_id)
        .with_for_update()
    )
    if user is None:
        raise _error(
            "service_team_staff_identity_unbound",
            "The service-team identity has no reviewed staff principal binding.",
            person_party_id=str(person_party_id),
        )
    validated, _person_party_id = _active_staff_identity(db, user.id)
    return validated


def _locked_team(db: Session, team_id: UUID) -> ServiceTeam:
    team = db.scalar(
        select(ServiceTeam).where(ServiceTeam.id == team_id).with_for_update()
    )
    if team is None:
        raise _error(
            "service_team_not_found",
            "Service team was not found.",
            team_id=str(team_id),
        )
    return team


def _locked_member(
    db: Session,
    *,
    team_id: UUID,
    member_id: UUID,
) -> ServiceTeamMember:
    member = db.scalar(
        select(ServiceTeamMember)
        .where(
            ServiceTeamMember.id == member_id,
            ServiceTeamMember.team_id == team_id,
        )
        .with_for_update()
    )
    if member is None:
        raise _error(
            "service_team_member_not_found",
            "Service-team member was not found.",
            team_id=str(team_id),
            member_id=str(member_id),
        )
    return member


def _ensure_name_available(
    db: Session,
    *,
    name: str,
    excluding_team_id: UUID | None = None,
) -> None:
    statement = select(ServiceTeam.id).where(
        func.lower(ServiceTeam.name) == name.lower()
    )
    if excluding_team_id is not None:
        statement = statement.where(ServiceTeam.id != excluding_team_id)
    if db.scalar(statement) is not None:
        raise _error(
            "service_team_name_conflict",
            "Another service team already uses this name.",
            name=name,
        )


def _utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_timestamp(left: datetime, right: datetime) -> bool:
    return _utc_instant(left) == _utc_instant(right)


def _set_team_composition(
    db: Session,
    *,
    team: ServiceTeam,
    capability_keys: tuple[ServiceTeamCapabilityKey, ...],
    geo_area_ids: tuple[UUID, ...],
) -> None:
    existing_capabilities = {
        item.capability_key: item
        for item in db.scalars(
            select(ServiceTeamCapability)
            .where(ServiceTeamCapability.team_id == team.id)
            .with_for_update()
        ).all()
    }
    desired_capabilities = {item.value for item in capability_keys}
    for key, capability_row in existing_capabilities.items():
        capability_row.is_active = key in desired_capabilities
    for key in desired_capabilities - set(existing_capabilities):
        db.add(
            ServiceTeamCapability(
                team_id=team.id,
                capability_key=key,
                is_active=True,
            )
        )

    existing_scopes = {
        item.geo_area_id: item
        for item in db.scalars(
            select(ServiceTeamScopeBinding)
            .where(
                ServiceTeamScopeBinding.team_id == team.id,
                ServiceTeamScopeBinding.scope_type
                == ServiceTeamScopeType.geo_area.value,
            )
            .with_for_update()
        ).all()
    }
    desired_geo_areas = set(geo_area_ids)
    for geo_area_id, scope_row in existing_scopes.items():
        scope_row.is_active = geo_area_id in desired_geo_areas
    for geo_area_id in desired_geo_areas - set(existing_scopes):
        db.add(
            ServiceTeamScopeBinding(
                team_id=team.id,
                scope_type=ServiceTeamScopeType.geo_area.value,
                geo_area_id=geo_area_id,
                is_active=True,
            )
        )


def _set_member_responsibilities(
    db: Session,
    *,
    member: ServiceTeamMember,
    responsibility_keys: tuple[ServiceTeamResponsibilityKey, ...],
    now: datetime,
) -> None:
    existing = {
        item.responsibility_key: item
        for item in db.scalars(
            select(ServiceTeamMemberResponsibility)
            .where(ServiceTeamMemberResponsibility.membership_id == member.id)
            .with_for_update()
        ).all()
    }
    desired = {item.value for item in responsibility_keys}
    for key, row in existing.items():
        active = key in desired
        row.is_active = active
        row.ended_at = None if active else (row.ended_at or now)
    for key in desired - set(existing):
        db.add(
            ServiceTeamMemberResponsibility(
                membership_id=member.id,
                responsibility_key=key,
                is_active=True,
                assigned_at=now,
            )
        )


def _active_capability_keys(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> dict[UUID, tuple[ServiceTeamCapabilityKey, ...]]:
    by_team: dict[UUID, list[ServiceTeamCapabilityKey]] = {
        team_id: [] for team_id in team_ids
    }
    if not team_ids:
        return {}
    for team_id, key in db.execute(
        select(
            ServiceTeamCapability.team_id,
            ServiceTeamCapability.capability_key,
        )
        .where(
            ServiceTeamCapability.team_id.in_(team_ids),
            ServiceTeamCapability.is_active.is_(True),
        )
        .order_by(
            ServiceTeamCapability.team_id.asc(),
            ServiceTeamCapability.capability_key.asc(),
        )
    ).all():
        by_team[team_id].append(ServiceTeamCapabilityKey(key))
    return {team_id: tuple(keys) for team_id, keys in by_team.items()}


def _audit_and_event(
    db: Session,
    *,
    context: CommandContext,
    action: str,
    team: ServiceTeam,
    member: ServiceTeamMember | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    actor_type, actor_id = _actor(context)
    evidence = {
        "schema_version": 1,
        "owner": OWNER,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "operation": action,
        "team_id": str(team.id),
        "member_id": str(member.id) if member is not None else None,
        "person_id": str(member.person_id) if member is not None else None,
        **(metadata or {}),
    }
    stage_audit_event(
        db,
        action=f"service_team.{action}",
        entity_type="service_team",
        entity_id=str(team.id),
        actor_type=actor_type,
        actor_id=actor_id,
        metadata=evidence,
    )
    emit_event(
        db,
        (
            EventType.service_team_membership_changed
            if member is not None
            else EventType.service_team_changed
        ),
        {
            **evidence,
            "aggregate_type": "service_team",
            "aggregate_id": str(team.id),
            "aggregate_version": str(context.command_id),
        },
        actor=context.actor,
    )


def create_team(db: Session, command: CreateServiceTeam) -> ServiceTeamMutation:
    """Create one native team; exact caller-supplied team-id replays are stable."""

    name = _clean_name(command.name)
    capability_keys = _capability_keys(command.capability_keys)
    geo_area_ids = _geo_area_ids(command.geo_area_ids)

    def apply() -> ServiceTeamMutation:
        _validate_registered_capabilities(db, capability_keys)
        _validate_geo_areas(db, geo_area_ids)
        existing = db.scalar(
            select(ServiceTeam)
            .where(ServiceTeam.id == command.team_id)
            .with_for_update()
        )
        if existing is not None:
            current_capabilities = set(
                db.scalars(
                    select(ServiceTeamCapability.capability_key).where(
                        ServiceTeamCapability.team_id == existing.id,
                        ServiceTeamCapability.is_active.is_(True),
                    )
                ).all()
            )
            current_geo_areas = set(
                db.scalars(
                    select(ServiceTeamScopeBinding.geo_area_id).where(
                        ServiceTeamScopeBinding.team_id == existing.id,
                        ServiceTeamScopeBinding.is_active.is_(True),
                    )
                ).all()
            )
            if (
                existing.name == name
                and existing.is_active
                and (
                    current_capabilities == {item.value for item in capability_keys}
                    and current_geo_areas == set(geo_area_ids)
                )
            ):
                return ServiceTeamMutation(existing.id, None, "created", replayed=True)
            raise _error(
                "service_team_identity_collision",
                "The service-team identifier is already bound to different values.",
                team_id=str(command.team_id),
            )
        _ensure_name_available(db, name=name)
        team = ServiceTeam(
            id=command.team_id,
            name=name,
            # Expand/shadow compatibility only. No consumer may derive behavior
            # from this sentinel.
            team_type="composable",
            region=None,
            manager_person_id=None,
            is_active=True,
        )
        db.add(team)
        db.flush()
        _set_team_composition(
            db,
            team=team,
            capability_keys=capability_keys,
            geo_area_ids=geo_area_ids,
        )
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="created",
            team=team,
            metadata={
                "capability_keys": tuple(item.value for item in capability_keys),
                "geo_area_ids": tuple(str(item) for item in geo_area_ids),
            },
        )
        return ServiceTeamMutation(team.id, None, "created", replayed=False)

    return execute_owner_command(
        db,
        definition=_CREATE,
        context=command.context,
        operation=apply,
    )


def update_team(db: Session, command: UpdateServiceTeam) -> ServiceTeamMutation:
    """Update team identity and composed capability/scope bindings."""

    name = _clean_name(command.name)
    capability_keys = _capability_keys(command.capability_keys)
    geo_area_ids = _geo_area_ids(command.geo_area_ids)

    def apply() -> ServiceTeamMutation:
        _validate_registered_capabilities(db, capability_keys)
        _validate_geo_areas(db, geo_area_ids)
        team = _locked_team(db, command.team_id)
        current_capabilities = set(
            db.scalars(
                select(ServiceTeamCapability.capability_key).where(
                    ServiceTeamCapability.team_id == team.id,
                    ServiceTeamCapability.is_active.is_(True),
                )
            ).all()
        )
        current_geo_areas = set(
            db.scalars(
                select(ServiceTeamScopeBinding.geo_area_id).where(
                    ServiceTeamScopeBinding.team_id == team.id,
                    ServiceTeamScopeBinding.is_active.is_(True),
                )
            ).all()
        )
        desired_capabilities = {item.value for item in capability_keys}
        desired_geo_areas = set(geo_area_ids)
        if (
            team.name == name
            and current_capabilities == desired_capabilities
            and current_geo_areas == desired_geo_areas
        ):
            return ServiceTeamMutation(team.id, None, "updated", replayed=True)
        if not _same_timestamp(team.updated_at, command.expected_updated_at):
            raise _error(
                "service_team_stale",
                "The service team changed after this form was loaded.",
                team_id=str(team.id),
            )
        _ensure_name_available(db, name=name, excluding_team_id=team.id)
        previous: dict[str, object] = {
            "previous_name": team.name,
            "previous_capability_keys": tuple(sorted(current_capabilities)),
            "previous_geo_area_ids": tuple(
                str(item) for item in sorted(current_geo_areas, key=str)
            ),
        }
        team.name = name
        _set_team_composition(
            db,
            team=team,
            capability_keys=capability_keys,
            geo_area_ids=geo_area_ids,
        )
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="updated",
            team=team,
            metadata={
                **previous,
                "capability_keys": tuple(item.value for item in capability_keys),
                "geo_area_ids": tuple(str(item) for item in geo_area_ids),
            },
        )
        return ServiceTeamMutation(team.id, None, "updated", replayed=False)

    return execute_owner_command(
        db,
        definition=_UPDATE,
        context=command.context,
        operation=apply,
    )


def set_team_active(
    db: Session,
    command: SetServiceTeamActive,
) -> ServiceTeamMutation:
    """Activate or deactivate a team without deleting historical identity."""

    reason = _clean_reason(command.reason)

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        operation = "activated" if command.is_active else "deactivated"
        if team.is_active is command.is_active:
            return ServiceTeamMutation(team.id, None, operation, replayed=True)
        if not _same_timestamp(team.updated_at, command.expected_updated_at):
            raise _error(
                "service_team_stale",
                "The service team changed after this action was loaded.",
                team_id=str(team.id),
            )
        if not command.is_active:
            active_members = db.scalar(
                select(func.count(ServiceTeamMember.id)).where(
                    ServiceTeamMember.team_id == team.id,
                    ServiceTeamMember.is_active.is_(True),
                )
            )
            if active_members:
                raise _error(
                    "service_team_has_active_members",
                    "Remove or transfer every active member before deactivating the team.",
                    team_id=str(team.id),
                    active_member_count=int(active_members),
                )
        team.is_active = command.is_active
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action=operation,
            team=team,
            metadata={"reason": reason},
        )
        return ServiceTeamMutation(team.id, None, operation, replayed=False)

    return execute_owner_command(
        db,
        definition=_SET_ACTIVE,
        context=command.context,
        operation=apply,
    )


def add_member(
    db: Session,
    command: AddServiceTeamMember,
) -> ServiceTeamMutation:
    """Add or reactivate one active staff member in one active team."""

    responsibility_keys = _responsibility_keys(command.responsibility_keys)

    def apply() -> ServiceTeamMutation:
        _validate_registered_responsibilities(db, responsibility_keys)
        team = _locked_team(db, command.team_id)
        if not team.is_active:
            raise _error(
                "service_team_inactive",
                "Members cannot be added to an inactive service team.",
                team_id=str(team.id),
            )
        _, person_party_id = _active_staff_identity(db, command.system_user_id)
        member = db.scalar(
            select(ServiceTeamMember)
            .where(
                ServiceTeamMember.team_id == team.id,
                ServiceTeamMember.person_id == person_party_id,
            )
            .with_for_update()
        )
        if member is not None and member.is_active:
            current_responsibilities = set(
                db.scalars(
                    select(ServiceTeamMemberResponsibility.responsibility_key).where(
                        ServiceTeamMemberResponsibility.membership_id == member.id,
                        ServiceTeamMemberResponsibility.is_active.is_(True),
                    )
                ).all()
            )
            if current_responsibilities == {item.value for item in responsibility_keys}:
                return ServiceTeamMutation(
                    team.id, member.id, "member_added", replayed=True
                )
        if member is None:
            member = ServiceTeamMember(
                team_id=team.id,
                person_id=person_party_id,
                role=ServiceTeamMemberRole.member.value,
                is_active=True,
            )
            db.add(member)
        else:
            member.is_active = True
        db.flush()
        _set_member_responsibilities(
            db,
            member=member,
            responsibility_keys=responsibility_keys,
            now=datetime.now(UTC),
        )
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_added",
            team=team,
            member=member,
            metadata={
                "responsibility_keys": tuple(item.value for item in responsibility_keys)
            },
        )
        return ServiceTeamMutation(team.id, member.id, "member_added", replayed=False)

    return execute_owner_command(
        db,
        definition=_ADD_MEMBER,
        context=command.context,
        operation=apply,
    )


def set_member_responsibilities(
    db: Session,
    command: SetServiceTeamMemberResponsibilities,
) -> ServiceTeamMutation:
    """Replace the composed responsibilities of one active membership."""

    responsibility_keys = _responsibility_keys(command.responsibility_keys)

    def apply() -> ServiceTeamMutation:
        _validate_registered_responsibilities(db, responsibility_keys)
        team = _locked_team(db, command.team_id)
        member = _locked_member(
            db,
            team_id=team.id,
            member_id=command.member_id,
        )
        if not member.is_active:
            raise _error(
                "service_team_member_inactive",
                "Reactivate the member before changing their role.",
                member_id=str(member.id),
            )
        _active_staff_for_person_party(db, member.person_id)
        current = set(
            db.scalars(
                select(ServiceTeamMemberResponsibility.responsibility_key).where(
                    ServiceTeamMemberResponsibility.membership_id == member.id,
                    ServiceTeamMemberResponsibility.is_active.is_(True),
                )
            ).all()
        )
        desired = {item.value for item in responsibility_keys}
        if current == desired:
            return ServiceTeamMutation(
                team.id, member.id, "member_updated", replayed=True
            )
        _set_member_responsibilities(
            db,
            member=member,
            responsibility_keys=responsibility_keys,
            now=datetime.now(UTC),
        )
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_updated",
            team=team,
            member=member,
            metadata={
                "previous_responsibility_keys": tuple(sorted(current)),
                "responsibility_keys": tuple(sorted(desired)),
            },
        )
        return ServiceTeamMutation(team.id, member.id, "member_updated", replayed=False)

    return execute_owner_command(
        db,
        definition=_UPDATE_MEMBER,
        context=command.context,
        operation=apply,
    )


def remove_member(
    db: Session,
    command: RemoveServiceTeamMember,
) -> ServiceTeamMutation:
    """Deactivate one membership while retaining historical identity."""

    reason = _clean_reason(command.reason)

    def apply() -> ServiceTeamMutation:
        team = _locked_team(db, command.team_id)
        member = _locked_member(
            db,
            team_id=team.id,
            member_id=command.member_id,
        )
        if not member.is_active:
            return ServiceTeamMutation(
                team.id, member.id, "member_removed", replayed=True
            )
        member.is_active = False
        now = datetime.now(UTC)
        for responsibility in db.scalars(
            select(ServiceTeamMemberResponsibility)
            .where(
                ServiceTeamMemberResponsibility.membership_id == member.id,
                ServiceTeamMemberResponsibility.is_active.is_(True),
            )
            .with_for_update()
        ).all():
            responsibility.is_active = False
            responsibility.ended_at = now
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="member_removed",
            team=team,
            member=member,
            metadata={"reason": reason},
        )
        return ServiceTeamMutation(team.id, member.id, "member_removed", replayed=False)

    return execute_owner_command(
        db,
        definition=_REMOVE_MEMBER,
        context=command.context,
        operation=apply,
    )


def _bounded_reference_value(
    value: str,
    *,
    field: str,
    maximum: int,
) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned or len(cleaned) > maximum:
        raise _error(
            "service_team_external_reference_invalid",
            f"{field} is required and must be at most {maximum} characters.",
            field=field,
        )
    return cleaned


def set_team_relationship(
    db: Session,
    command: SetServiceTeamRelationship,
) -> ServiceTeamTopologyMutation:
    if command.parent_team_id == command.child_team_id:
        raise _error(
            "service_team_relationship_invalid",
            "A service team cannot be its own parent.",
        )

    def apply() -> ServiceTeamTopologyMutation:
        parent = _locked_team(db, command.parent_team_id)
        child = _locked_team(db, command.child_team_id)
        relationship = db.scalar(
            select(ServiceTeamRelationship)
            .where(ServiceTeamRelationship.id == command.relationship_id)
            .with_for_update()
        )
        desired = (
            parent.id,
            child.id,
            command.relationship_type.value,
            command.is_active,
        )
        if relationship is not None:
            current = (
                relationship.parent_team_id,
                relationship.child_team_id,
                relationship.relationship_type,
                relationship.is_active,
            )
            if current == desired:
                return ServiceTeamTopologyMutation(
                    parent.id,
                    relationship.id,
                    "relationship_set",
                    replayed=True,
                )
            if current[:3] != desired[:3]:
                raise _error(
                    "service_team_relationship_identity_collision",
                    "The relationship identifier is already bound to another edge.",
                    relationship_id=str(relationship.id),
                )
        if command.is_active:
            edges = {
                (row.parent_team_id, row.child_team_id)
                for row in db.scalars(
                    select(ServiceTeamRelationship)
                    .where(
                        ServiceTeamRelationship.is_active.is_(True),
                        ServiceTeamRelationship.id != command.relationship_id,
                    )
                    .with_for_update()
                ).all()
            }
            edges.add((parent.id, child.id))
            adjacency: dict[UUID, set[UUID]] = {}
            for source, target in edges:
                adjacency.setdefault(source, set()).add(target)
            pending = [child.id]
            visited: set[UUID] = set()
            while pending:
                node = pending.pop()
                if node == parent.id:
                    raise _error(
                        "service_team_relationship_cycle",
                        "The relationship would create a team hierarchy cycle.",
                    )
                if node in visited:
                    continue
                visited.add(node)
                pending.extend(adjacency.get(node, set()))
        if relationship is None:
            relationship = ServiceTeamRelationship(
                id=command.relationship_id,
                parent_team_id=parent.id,
                child_team_id=child.id,
                relationship_type=command.relationship_type.value,
                is_active=command.is_active,
            )
            db.add(relationship)
        else:
            relationship.is_active = command.is_active
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="relationship_set",
            team=parent,
            metadata={
                "relationship_id": str(relationship.id),
                "child_team_id": str(child.id),
                "relationship_type": relationship.relationship_type,
                "is_active": relationship.is_active,
            },
        )
        return ServiceTeamTopologyMutation(
            parent.id,
            relationship.id,
            "relationship_set",
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_SET_RELATIONSHIP,
        context=command.context,
        operation=apply,
    )


def observe_external_reference(
    db: Session,
    command: ObserveServiceTeamExternalReference,
) -> ServiceTeamTopologyMutation:
    system = _bounded_reference_value(command.system, field="system", maximum=80)
    entity_type = _bounded_reference_value(
        command.entity_type,
        field="entity_type",
        maximum=80,
    )
    external_reference = _bounded_reference_value(
        command.external_reference,
        field="external_reference",
        maximum=200,
    )
    observed_at = _utc_instant(command.observed_at)

    def apply() -> ServiceTeamTopologyMutation:
        team = _locked_team(db, command.team_id)
        conflicting = db.scalar(
            select(ServiceTeamExternalReference)
            .where(
                ServiceTeamExternalReference.system == system,
                ServiceTeamExternalReference.entity_type == entity_type,
                ServiceTeamExternalReference.external_reference == external_reference,
                ServiceTeamExternalReference.id != command.reference_id,
            )
            .with_for_update()
        )
        if conflicting is not None:
            raise _error(
                "service_team_external_reference_conflict",
                "The external reference is already observed for another team.",
                conflicting_team_id=str(conflicting.team_id),
            )
        conflicting_kind = db.scalar(
            select(ServiceTeamExternalReference)
            .where(
                ServiceTeamExternalReference.team_id == team.id,
                ServiceTeamExternalReference.system == system,
                ServiceTeamExternalReference.entity_type == entity_type,
                ServiceTeamExternalReference.id != command.reference_id,
            )
            .with_for_update()
        )
        if conflicting_kind is not None:
            raise _error(
                "service_team_external_reference_kind_conflict",
                "This team already has an observation for the provider entity type.",
                conflicting_reference_id=str(conflicting_kind.id),
            )
        reference = db.scalar(
            select(ServiceTeamExternalReference)
            .where(ServiceTeamExternalReference.id == command.reference_id)
            .with_for_update()
        )
        desired = (
            team.id,
            system,
            entity_type,
            external_reference,
            observed_at,
            command.is_active,
        )
        if reference is not None:
            current = (
                reference.team_id,
                reference.system,
                reference.entity_type,
                reference.external_reference,
                _utc_instant(reference.observed_at),
                reference.is_active,
            )
            if current == desired:
                return ServiceTeamTopologyMutation(
                    team.id,
                    reference.id,
                    "external_reference_observed",
                    replayed=True,
                )
            if current[:4] != desired[:4]:
                raise _error(
                    "service_team_external_reference_identity_collision",
                    "The reference identifier is already bound to another observation.",
                    reference_id=str(reference.id),
                )
            reference.observed_at = observed_at
            reference.is_active = command.is_active
            reference.retired_at = None if command.is_active else observed_at
        else:
            reference = ServiceTeamExternalReference(
                id=command.reference_id,
                team_id=team.id,
                system=system,
                entity_type=entity_type,
                external_reference=external_reference,
                observed_at=observed_at,
                is_active=command.is_active,
                retired_at=None if command.is_active else observed_at,
            )
            db.add(reference)
        db.flush()
        _audit_and_event(
            db,
            context=command.context,
            action="external_reference_observed",
            team=team,
            metadata={
                "external_reference_id": str(reference.id),
                "system": system,
                "entity_type": entity_type,
                "external_reference_sha256": hashlib.sha256(
                    external_reference.encode()
                ).hexdigest(),
                "observed_at": observed_at.isoformat(),
                "is_active": command.is_active,
            },
        )
        return ServiceTeamTopologyMutation(
            team.id,
            reference.id,
            "external_reference_observed",
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_OBSERVE_EXTERNAL_REFERENCE,
        context=command.context,
        operation=apply,
    )


def _team_views(db: Session, teams: list[ServiceTeam]) -> tuple[ServiceTeamView, ...]:
    team_ids = tuple(team.id for team in teams)
    capability_keys = _active_capability_keys(db, team_ids)
    geo_areas_by_team: dict[UUID, list[tuple[UUID, str]]] = {
        team_id: [] for team_id in team_ids
    }
    for scoped_team_id, geo_area_id, geo_area_name in db.execute(
        select(
            ServiceTeamScopeBinding.team_id,
            GeoArea.id,
            GeoArea.name,
        )
        .join(GeoArea, GeoArea.id == ServiceTeamScopeBinding.geo_area_id)
        .where(
            ServiceTeamScopeBinding.team_id.in_(team_ids),
            ServiceTeamScopeBinding.is_active.is_(True),
            GeoArea.is_active.is_(True),
        )
        .order_by(
            ServiceTeamScopeBinding.team_id.asc(),
            GeoArea.name.asc(),
        )
    ).all():
        geo_areas_by_team[scoped_team_id].append((geo_area_id, geo_area_name))

    managers_by_team: dict[UUID, list[tuple[UUID, str]]] = {
        team_id: [] for team_id in team_ids
    }
    for manager_team_id, person_party_id, user in db.execute(
        select(
            ServiceTeamMember.team_id,
            ServiceTeamMember.person_id,
            SystemUser,
        )
        .join(
            ServiceTeamMemberResponsibility,
            ServiceTeamMemberResponsibility.membership_id == ServiceTeamMember.id,
        )
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .join(Party, Party.id == ServiceTeamMember.person_id)
        .where(
            ServiceTeamMember.team_id.in_(team_ids),
            ServiceTeamMember.is_active.is_(True),
            ServiceTeamMemberResponsibility.is_active.is_(True),
            ServiceTeamMemberResponsibility.responsibility_key
            == ServiceTeamResponsibilityKey.accountable_manager.value,
            SystemUser.is_active.is_(True),
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
        .order_by(ServiceTeamMember.team_id.asc(), SystemUser.id.asc())
    ).all():
        managers_by_team[manager_team_id].append((person_party_id, _staff_label(user)))

    member_counts = (
        {
            team_id: int(count)
            for team_id, count in db.execute(
                select(ServiceTeamMember.team_id, func.count(ServiceTeamMember.id))
                .where(
                    ServiceTeamMember.team_id.in_(team_ids),
                    ServiceTeamMember.is_active.is_(True),
                )
                .group_by(ServiceTeamMember.team_id)
            ).all()
        }
        if team_ids
        else {}
    )
    legacy_roles_by_team: dict[UUID, list[tuple[UUID, str]]] = {
        team_id: [] for team_id in team_ids
    }
    for legacy_team_id, membership_id, role in db.execute(
        select(
            ServiceTeamMember.team_id,
            ServiceTeamMember.id,
            ServiceTeamMember.role,
        ).where(
            ServiceTeamMember.team_id.in_(team_ids),
            ServiceTeamMember.is_active.is_(True),
            ServiceTeamMember.role != ServiceTeamMemberRole.member.value,
        )
    ).all():
        legacy_roles_by_team[legacy_team_id].append((membership_id, role))

    legacy_membership_ids = tuple(
        membership_id
        for rows in legacy_roles_by_team.values()
        for membership_id, _role in rows
    )
    responsibilities_by_membership: dict[UUID, set[str]] = {
        membership_id: set() for membership_id in legacy_membership_ids
    }
    if legacy_membership_ids:
        for membership_id, responsibility_key in db.execute(
            select(
                ServiceTeamMemberResponsibility.membership_id,
                ServiceTeamMemberResponsibility.responsibility_key,
            ).where(
                ServiceTeamMemberResponsibility.membership_id.in_(
                    legacy_membership_ids
                ),
                ServiceTeamMemberResponsibility.is_active.is_(True),
            )
        ).all():
            responsibilities_by_membership[membership_id].add(responsibility_key)

    legacy_capability_map = {
        "operations": {
            ServiceTeamCapabilityKey.operations_general,
            ServiceTeamCapabilityKey.network_outages_coordinate,
        },
        "support": {
            ServiceTeamCapabilityKey.support_tickets,
            ServiceTeamCapabilityKey.communications_inbox,
            ServiceTeamCapabilityKey.network_outages_observe,
        },
        "field_service": {
            ServiceTeamCapabilityKey.field_service_work_orders,
            ServiceTeamCapabilityKey.network_outages_observe,
        },
        "billing": {ServiceTeamCapabilityKey.billing_operations},
        "project_management": {ServiceTeamCapabilityKey.projects_manage},
    }
    legacy_role_map = {
        ServiceTeamMemberRole.lead.value: ServiceTeamResponsibilityKey.queue_lead.value,
        ServiceTeamMemberRole.manager.value: (
            ServiceTeamResponsibilityKey.accountable_manager.value
        ),
    }

    def legacy_shadow_issues(
        team: ServiceTeam,
    ) -> tuple[ServiceTeamLegacyShadowIssue, ...]:
        issues: set[ServiceTeamLegacyShadowIssue] = set()
        team_capabilities = set(capability_keys.get(team.id, ()))
        manager_person_ids = {
            person_id for person_id, _label in managers_by_team.get(team.id, ())
        }

        if (
            team.team_type != "composable"
            and legacy_capability_map.get(team.team_type, set()) != team_capabilities
        ):
            issues.add(ServiceTeamLegacyShadowIssue.team_type_capability_mismatch)
        if str(team.region or "").strip():
            issues.add(ServiceTeamLegacyShadowIssue.region_requires_geo_area_review)
        if (
            team.manager_person_id is not None
            and team.manager_person_id not in manager_person_ids
        ):
            issues.add(
                ServiceTeamLegacyShadowIssue.manager_requires_explicit_composition
            )
        if any(
            legacy_role_map.get(role)
            not in responsibilities_by_membership.get(membership_id, set())
            for membership_id, role in legacy_roles_by_team.get(team.id, ())
        ):
            issues.add(ServiceTeamLegacyShadowIssue.member_role_responsibility_mismatch)

        return tuple(issue for issue in ServiceTeamLegacyShadowIssue if issue in issues)

    return tuple(
        ServiceTeamView(
            team_id=team.id,
            name=team.name,
            capabilities=capability_keys.get(team.id, ()),
            geo_areas=tuple(geo_areas_by_team.get(team.id, ())),
            accountable_manager_labels=tuple(
                label for _person_id, label in managers_by_team.get(team.id, ())
            ),
            legacy_shadow_issues=legacy_shadow_issues(team),
            is_active=team.is_active,
            active_member_count=member_counts.get(team.id, 0),
            created_at=team.created_at,
            updated_at=team.updated_at,
        )
        for team in teams
    )


def audit_legacy_service_team_shadow(
    db: Session,
) -> ServiceTeamLegacyShadowAudit:
    """Verify retained scalars against the transaction-current composed model."""

    teams = list(db.scalars(select(ServiceTeam).order_by(ServiceTeam.id.asc())).all())
    views = _team_views(db, teams)
    return ServiceTeamLegacyShadowAudit(
        team_count=len(views),
        drift_team_count=sum(view.legacy_shadow_drift for view in views),
        issue_counts=tuple(
            (
                issue,
                sum(issue in view.legacy_shadow_issues for view in views),
            )
            for issue in ServiceTeamLegacyShadowIssue
        ),
    )


def list_teams(
    db: Session,
    *,
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 100,
) -> ServiceTeamList:
    """Return one bounded operator list from native records."""

    normalized_search = " ".join(str(search or "").split())
    normalized_offset = max(int(offset), 0)
    normalized_limit = min(max(int(limit), 1), 200)
    filters = []
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                ServiceTeam.name.ilike(pattern),
                ServiceTeam.id.in_(
                    select(ServiceTeamCapability.team_id).where(
                        ServiceTeamCapability.capability_key.ilike(pattern),
                        ServiceTeamCapability.is_active.is_(True),
                    )
                ),
                ServiceTeam.id.in_(
                    select(ServiceTeamScopeBinding.team_id)
                    .join(
                        GeoArea,
                        GeoArea.id == ServiceTeamScopeBinding.geo_area_id,
                    )
                    .where(
                        GeoArea.name.ilike(pattern),
                        ServiceTeamScopeBinding.is_active.is_(True),
                    )
                ),
            )
        )
    if is_active is not None:
        filters.append(ServiceTeam.is_active.is_(is_active))
    statement = select(ServiceTeam)
    count_statement = select(func.count(ServiceTeam.id))
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)
    teams = db.scalars(
        statement.order_by(ServiceTeam.is_active.desc(), ServiceTeam.name.asc())
        .offset(normalized_offset)
        .limit(normalized_limit)
    ).all()
    active_count, inactive_count = db.execute(
        select(
            func.count(ServiceTeam.id).filter(ServiceTeam.is_active.is_(True)),
            func.count(ServiceTeam.id).filter(ServiceTeam.is_active.is_(False)),
        )
    ).one()
    return ServiceTeamList(
        items=_team_views(db, list(teams)),
        total=int(db.scalar(count_statement) or 0),
        active_count=int(active_count),
        inactive_count=int(inactive_count),
        search=normalized_search,
        active_filter=is_active,
        offset=normalized_offset,
        limit=normalized_limit,
    )


def get_team(db: Session, team_id: UUID) -> ServiceTeamDetail:
    """Return one team, memberships, and eligible staff from native records."""

    team = db.get(ServiceTeam, team_id)
    if team is None:
        raise _error(
            "service_team_not_found",
            "Service team was not found.",
            team_id=str(team_id),
        )
    member_rows = db.scalars(
        select(ServiceTeamMember)
        .where(ServiceTeamMember.team_id == team.id)
        .order_by(
            ServiceTeamMember.is_active.desc(),
            ServiceTeamMember.created_at.asc(),
        )
    ).all()
    person_ids = {member.person_id for member in member_rows}
    staff = (
        {
            user.person_party_id: user
            for user in db.scalars(
                select(SystemUser).where(SystemUser.person_party_id.in_(person_ids))
            ).all()
        }
        if person_ids
        else {}
    )
    active_party_ids = (
        set(
            db.scalars(
                select(Party.id).where(
                    Party.id.in_(person_ids),
                    Party.party_type == PartyType.person.value,
                    Party.status == PartyIdentityStatus.active.value,
                )
            ).all()
        )
        if person_ids
        else set()
    )
    active_staff_person_ids = {
        person_party_id
        for person_party_id, user in staff.items()
        if user.is_active and person_party_id in active_party_ids
    }
    responsibility_rows = db.execute(
        select(
            ServiceTeamMemberResponsibility.membership_id,
            ServiceTeamMemberResponsibility.responsibility_key,
        )
        .where(
            ServiceTeamMemberResponsibility.membership_id.in_(
                tuple(member.id for member in member_rows)
            ),
            ServiceTeamMemberResponsibility.is_active.is_(True),
        )
        .order_by(
            ServiceTeamMemberResponsibility.membership_id.asc(),
            ServiceTeamMemberResponsibility.responsibility_key.asc(),
        )
    ).all()
    responsibilities_by_member: dict[UUID, list[ServiceTeamResponsibilityKey]] = {
        member.id: [] for member in member_rows
    }
    for membership_id, responsibility_key in responsibility_rows:
        responsibilities_by_member[membership_id].append(
            ServiceTeamResponsibilityKey(responsibility_key)
        )
    members = tuple(
        ServiceTeamMemberView(
            member_id=member.id,
            person_id=member.person_id,
            system_user_id=(
                staff[member.person_id].id if member.person_id in staff else None
            ),
            person_label=(
                _staff_label(staff[member.person_id])
                if member.person_id in staff
                else "Unknown staff"
            ),
            person_email=(
                staff[member.person_id].email if member.person_id in staff else ""
            ),
            responsibilities=tuple(responsibilities_by_member.get(member.id, ())),
            is_active=member.is_active,
            staff_identity_active=member.person_id in active_staff_person_ids,
            created_at=member.created_at,
        )
        for member in member_rows
    )
    active_member_ids = {member.person_id for member in member_rows if member.is_active}
    available_staff = tuple(
        _staff_option(user)
        for user in db.scalars(
            select(SystemUser)
            .join(Party, Party.id == SystemUser.person_party_id)
            .where(
                SystemUser.is_active.is_(True),
                SystemUser.person_party_id.is_not(None),
                SystemUser.person_party_id.not_in(active_member_ids),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .order_by(
                SystemUser.first_name.asc(),
                SystemUser.last_name.asc(),
                SystemUser.email.asc(),
            )
        ).all()
    )
    team_view = _team_views(db, [team])[0]
    if team.is_active:
        can_deactivate = team_view.active_member_count == 0
        actions = ServiceTeamActionEligibility(
            can_edit=True,
            can_add_member=True,
            can_activate=False,
            can_deactivate=can_deactivate,
            lifecycle_block_reason=(
                None
                if can_deactivate
                else "Remove or transfer every active member before deactivation."
            ),
        )
    else:
        actions = ServiceTeamActionEligibility(
            can_edit=True,
            can_add_member=False,
            can_activate=True,
            can_deactivate=False,
            lifecycle_block_reason=None,
        )
    return ServiceTeamDetail(
        team=team_view,
        members=members,
        available_staff=available_staff,
        actions=actions,
    )


def list_staff_options(db: Session) -> tuple[StaffOption, ...]:
    """Return active staff principals available to manage or join teams."""

    return tuple(
        _staff_option(user)
        for user in db.scalars(
            select(SystemUser)
            .join(Party, Party.id == SystemUser.person_party_id)
            .where(
                SystemUser.is_active.is_(True),
                SystemUser.person_party_id.is_not(None),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .order_by(
                SystemUser.first_name.asc(),
                SystemUser.last_name.asc(),
                SystemUser.email.asc(),
            )
        ).all()
    )


def list_capability_options(
    db: Session,
) -> tuple[tuple[ServiceTeamCapabilityKey, str], ...]:
    return tuple(
        (ServiceTeamCapabilityKey(row.key), row.name)
        for row in db.scalars(
            select(ServiceTeamCapabilityDefinition)
            .where(ServiceTeamCapabilityDefinition.is_active.is_(True))
            .order_by(ServiceTeamCapabilityDefinition.name.asc())
        ).all()
    )


def list_responsibility_options(
    db: Session,
) -> tuple[tuple[ServiceTeamResponsibilityKey, str], ...]:
    return tuple(
        (ServiceTeamResponsibilityKey(row.key), row.name)
        for row in db.scalars(
            select(ServiceTeamResponsibilityDefinition)
            .where(ServiceTeamResponsibilityDefinition.is_active.is_(True))
            .order_by(ServiceTeamResponsibilityDefinition.name.asc())
        ).all()
    )


def list_geo_area_options(db: Session) -> tuple[tuple[UUID, str], ...]:
    return tuple(
        (area.id, area.name)
        for area in db.scalars(
            select(GeoArea)
            .where(GeoArea.is_active.is_(True))
            .order_by(GeoArea.name.asc())
        ).all()
    )


def resolve_staff_service_teams(
    db: Session,
    system_user_id: UUID,
    *,
    capability_keys: tuple[ServiceTeamCapabilityKey, ...] = (),
    geo_area_ids: tuple[UUID, ...] = (),
) -> ServiceTeamResolution:
    """Resolve every matching active team for an active staff principal.

    Multi-team membership is valid. Consumers either keep the returned set or
    use an authoritative work/routing assignment to select one exact team.
    """

    normalized_capabilities = tuple(sorted(set(capability_keys), key=lambda x: x.value))
    normalized_geo_areas = tuple(sorted(set(geo_area_ids), key=str))
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active or user.person_party_id is None:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=(user.person_party_id if user is not None else None),
            kind=ServiceTeamResolutionKind.identity_unavailable,
            team_ids=(),
        )
    person_party_id = user.person_party_id
    party_is_active = db.scalar(
        select(Party.id).where(
            Party.id == person_party_id,
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if party_is_active is None:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            kind=ServiceTeamResolutionKind.identity_unavailable,
            team_ids=(),
        )
    statement = (
        select(ServiceTeam.id)
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .where(
            ServiceTeamMember.person_id == person_party_id,
            ServiceTeamMember.is_active.is_(True),
            ServiceTeam.is_active.is_(True),
        )
    )
    for capability_key in normalized_capabilities:
        statement = statement.where(
            ServiceTeam.id.in_(
                select(ServiceTeamCapability.team_id).where(
                    ServiceTeamCapability.capability_key == capability_key.value,
                    ServiceTeamCapability.is_active.is_(True),
                )
            )
        )
    if normalized_geo_areas:
        statement = statement.where(
            ServiceTeam.id.in_(
                select(ServiceTeamScopeBinding.team_id).where(
                    ServiceTeamScopeBinding.geo_area_id.in_(normalized_geo_areas),
                    ServiceTeamScopeBinding.is_active.is_(True),
                )
            )
        )
    candidate_team_ids = tuple(
        db.scalars(statement.distinct().order_by(ServiceTeam.id.asc())).all()
    )
    if not candidate_team_ids:
        return ServiceTeamResolution(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            kind=ServiceTeamResolutionKind.no_membership,
            team_ids=(),
        )
    return ServiceTeamResolution(
        system_user_id=system_user_id,
        person_party_id=person_party_id,
        kind=ServiceTeamResolutionKind.resolved,
        team_ids=candidate_team_ids,
    )


def team_ids_with_capability(
    db: Session,
    capability_key: ServiceTeamCapabilityKey,
) -> tuple[UUID, ...]:
    """Return all active teams registered for one governed capability."""

    return tuple(
        db.scalars(
            select(ServiceTeam.id)
            .join(
                ServiceTeamCapability,
                ServiceTeamCapability.team_id == ServiceTeam.id,
            )
            .where(
                ServiceTeam.is_active.is_(True),
                ServiceTeamCapability.is_active.is_(True),
                ServiceTeamCapability.capability_key == capability_key.value,
            )
            .order_by(ServiceTeam.id.asc())
        ).all()
    )


def resolve_staff_team_scope(
    db: Session,
    system_user_id: UUID,
) -> StaffServiceTeamScope:
    """Resolve membership and responsibility scope for one staff principal.

    Workqueue, Inbox, ticket, and dispatch callers consume this query instead of
    comparing adapter-facing ``SystemUser.id`` values with Party-backed
    ``ServiceTeamMember.person_id`` values themselves.
    """

    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active or user.person_party_id is None:
        return StaffServiceTeamScope(
            system_user_id=system_user_id,
            person_party_id=(user.person_party_id if user is not None else None),
            identity_available=False,
            member_team_ids=(),
            queue_lead_team_ids=(),
            accountable_manager_team_ids=(),
        )
    person_party_id = user.person_party_id
    party_is_active = db.scalar(
        select(Party.id).where(
            Party.id == person_party_id,
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if party_is_active is None:
        return StaffServiceTeamScope(
            system_user_id=system_user_id,
            person_party_id=person_party_id,
            identity_available=False,
            member_team_ids=(),
            queue_lead_team_ids=(),
            accountable_manager_team_ids=(),
        )

    memberships = db.execute(
        select(
            ServiceTeam.id,
            ServiceTeamMemberResponsibility.responsibility_key,
        )
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .outerjoin(
            ServiceTeamMemberResponsibility,
            (ServiceTeamMemberResponsibility.membership_id == ServiceTeamMember.id)
            & ServiceTeamMemberResponsibility.is_active.is_(True),
        )
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            ServiceTeamMember.person_id == person_party_id,
        )
        .order_by(ServiceTeam.id.asc())
    ).all()
    member_team_ids = tuple(sorted({row[0] for row in memberships}, key=str))
    queue_lead_team_ids = tuple(
        sorted(
            {
                row[0]
                for row in memberships
                if row[1] == ServiceTeamResponsibilityKey.queue_lead.value
            },
            key=str,
        )
    )
    accountable_manager_team_ids = tuple(
        sorted(
            {
                row[0]
                for row in memberships
                if row[1] == ServiceTeamResponsibilityKey.accountable_manager.value
            },
            key=str,
        )
    )
    return StaffServiceTeamScope(
        system_user_id=system_user_id,
        person_party_id=person_party_id,
        identity_available=True,
        member_team_ids=member_team_ids,
        queue_lead_team_ids=queue_lead_team_ids,
        accountable_manager_team_ids=accountable_manager_team_ids,
    )


def list_active_team_member_system_user_ids(
    db: Session,
    team_ids: frozenset[UUID] | set[UUID] | tuple[UUID, ...],
) -> tuple[UUID, ...]:
    """Return active authenticated staff IDs for active native teams."""

    normalized_team_ids = tuple(sorted(set(team_ids), key=str))
    if not normalized_team_ids:
        return ()
    return tuple(
        db.scalars(
            select(SystemUser.id)
            .join(
                ServiceTeamMember,
                ServiceTeamMember.person_id == SystemUser.person_party_id,
            )
            .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
            .join(Party, Party.id == ServiceTeamMember.person_id)
            .where(
                ServiceTeam.id.in_(normalized_team_ids),
                ServiceTeam.is_active.is_(True),
                ServiceTeamMember.is_active.is_(True),
                SystemUser.is_active.is_(True),
                Party.party_type == PartyType.person.value,
                Party.status == PartyIdentityStatus.active.value,
            )
            .distinct()
            .order_by(SystemUser.id.asc())
        ).all()
    )


def list_active_team_options(db: Session) -> tuple[tuple[UUID, str], ...]:
    """Return the shared active-team selector projection."""

    return tuple(
        (team.id, team.name)
        for team in db.scalars(
            select(ServiceTeam)
            .where(ServiceTeam.is_active.is_(True))
            .order_by(ServiceTeam.name.asc())
        ).all()
    )


def list_responsibility_groups(
    db: Session,
    *,
    search: str | None = None,
    limit: int = 500,
) -> tuple[ServiceTeamResponsibilityGroup, ...]:
    """Group active Party-backed members by composed responsibility."""

    normalized_search = " ".join(str(search or "").split())
    normalized_limit = min(max(int(limit), 1), 1000)
    statement = (
        select(
            ServiceTeam,
            ServiceTeamMember,
            ServiceTeamMemberResponsibility,
            SystemUser,
        )
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .join(
            ServiceTeamMemberResponsibility,
            ServiceTeamMemberResponsibility.membership_id == ServiceTeamMember.id,
        )
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .join(Party, Party.id == ServiceTeamMember.person_id)
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            ServiceTeamMemberResponsibility.is_active.is_(True),
            SystemUser.is_active.is_(True),
            Party.party_type == PartyType.person.value,
            Party.status == PartyIdentityStatus.active.value,
        )
    )
    if normalized_search:
        pattern = f"%{normalized_search}%"
        statement = statement.where(
            or_(
                ServiceTeam.name.ilike(pattern),
                ServiceTeamMemberResponsibility.responsibility_key.ilike(pattern),
                SystemUser.display_name.ilike(pattern),
                SystemUser.first_name.ilike(pattern),
                SystemUser.last_name.ilike(pattern),
                SystemUser.email.ilike(pattern),
            )
        )
    rows = db.execute(
        statement.order_by(
            ServiceTeamMemberResponsibility.responsibility_key.asc(),
            SystemUser.first_name.asc(),
            SystemUser.last_name.asc(),
            SystemUser.email.asc(),
            ServiceTeam.name.asc(),
        ).limit(normalized_limit)
    ).all()
    grouped: dict[ServiceTeamResponsibilityKey, _ResponsibilityAccumulator] = {}
    for team, member, responsibility, user in rows:
        responsibility_key = ServiceTeamResponsibilityKey(
            responsibility.responsibility_key
        )
        accumulator = grouped.setdefault(
            responsibility_key,
            _ResponsibilityAccumulator(
                responsibility=responsibility_key,
                members={},
            ),
        )
        current = accumulator.members.get(member.person_id)
        if current is None:
            accumulator.members[member.person_id] = (user, {team.name})
        else:
            current[1].add(team.name)

    return tuple(
        ServiceTeamResponsibilityGroup(
            group_key=responsibility_key.value,
            responsibility=responsibility_key,
            members=tuple(
                ServiceTeamRoleRegionMember(
                    person_id=person_id,
                    system_user_id=user.id,
                    label=_staff_label(user),
                    email=user.email,
                    team_names=tuple(sorted(team_names, key=str.casefold)),
                )
                for person_id, (user, team_names) in sorted(
                    accumulator.members.items(),
                    key=lambda item: (
                        _staff_label(item[1][0]).casefold(),
                        item[1][0].email.casefold(),
                        str(item[0]),
                    ),
                )
            ),
        )
        for responsibility_key, accumulator in sorted(
            grouped.items(),
            key=lambda item: item[0].value,
        )
    )
