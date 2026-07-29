"""Narrow retirement gate for legacy service-team manager pointers.

This owner exists only so an installation still below migration 426 can retire
unresolvable CRM-era manager UUIDs without importing CRM People or memberships.
It never creates a Party, binds a principal, changes membership, or copies team
topology.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.service_team import ServiceTeam, ServiceTeamMember
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
from app.services.settings_spec import (
    resolve_legacy_service_team_cutover_settings,
)

OWNER = "operations.service_team_pointer_retirement"
READINESS_CONCERN = "legacy service-team pointer retirement readiness"
RETIREMENT_CONCERN = "approved legacy service-team pointer retirement"
PLAN_SCHEMA_VERSION = 1
APPROVAL_SCHEMA_VERSION = 1
MAX_POINTERS_PER_EXECUTION = 25
MAX_APPROVAL_WINDOW = timedelta(hours=24)
MAX_FUTURE_APPROVAL_SKEW = timedelta(minutes=5)

_RETIRE = OwnerCommandDefinition(
    owner=OWNER,
    concern=RETIREMENT_CONCERN,
    name="retire_legacy_service_team_pointers",
)


class ServiceTeamPointerRetirementError(DomainError):
    """Stable refusal of an unsafe pointer-retirement operation."""


@dataclass(frozen=True)
class ServiceTeamPointerRetirementAudit:
    team_count: int
    duplicate_casefolded_team_name_count: int
    manager_reference_count: int
    legacy_manager_pointer_count: int
    membership_count: int
    membership_blocker_count: int
    workflow_setting_blocker_count: int

    @property
    def blocker_count(self) -> int:
        return (
            self.duplicate_casefolded_team_name_count
            + self.legacy_manager_pointer_count
            + self.membership_blocker_count
            + self.workflow_setting_blocker_count
        )

    @property
    def ready(self) -> bool:
        return self.blocker_count == 0

    def summary(self) -> dict[str, int | bool]:
        return {
            "ready": self.ready,
            "blocker_count": self.blocker_count,
            "team_count": self.team_count,
            "duplicate_casefolded_team_name_count": (
                self.duplicate_casefolded_team_name_count
            ),
            "manager_reference_count": self.manager_reference_count,
            "legacy_manager_pointer_count": self.legacy_manager_pointer_count,
            "membership_count": self.membership_count,
            "membership_blocker_count": self.membership_blocker_count,
            "workflow_setting_blocker_count": self.workflow_setting_blocker_count,
        }


@dataclass(frozen=True)
class LegacyManagerPointer:
    team_id: UUID
    stored_person_id: UUID

    def payload(self) -> dict[str, str]:
        return {
            "team_id": str(self.team_id),
            "stored_person_id": str(self.stored_person_id),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise _error("invalid_plan", "A pointer plan row must be an object.")
        try:
            return cls(
                team_id=UUID(str(payload["team_id"])),
                stored_person_id=UUID(str(payload["stored_person_id"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "invalid_plan",
                "A pointer plan row is malformed.",
            ) from exc


@dataclass(frozen=True)
class ServiceTeamPointerRetirementPlan:
    source_snapshot_sha256: str
    planned_at: datetime
    pointers: tuple[LegacyManagerPointer, ...]
    schema_version: int = PLAN_SCHEMA_VERSION

    def digest_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "planned_at": _utc(self.planned_at).isoformat(),
            "pointers": [
                pointer.payload()
                for pointer in sorted(self.pointers, key=lambda row: str(row.team_id))
            ],
        }

    @property
    def plan_digest(self) -> str:
        return _payload_digest(self.digest_payload())

    def file_payload(self) -> dict[str, object]:
        return {**self.digest_payload(), "plan_digest": self.plan_digest}

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise _error("invalid_plan", "The pointer-retirement plan is malformed.")
        try:
            pointers = payload["pointers"]
            if not isinstance(pointers, list):
                raise TypeError
            plan = cls(
                schema_version=int(str(payload["schema_version"])),
                source_snapshot_sha256=str(payload["source_snapshot_sha256"]),
                planned_at=datetime.fromisoformat(
                    str(payload["planned_at"]).replace("Z", "+00:00")
                ),
                pointers=tuple(
                    LegacyManagerPointer.from_payload(item) for item in pointers
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ServiceTeamPointerRetirementError):
                raise
            raise _error(
                "invalid_plan",
                "The pointer-retirement plan is malformed.",
            ) from exc
        _validate_plan(plan)
        if str(payload.get("plan_digest") or "") != plan.plan_digest:
            raise _error(
                "invalid_plan",
                "The pointer-retirement plan digest does not match its content.",
            )
        return plan


@dataclass(frozen=True)
class ServiceTeamPointerRetirementApproval:
    plan_digest: str
    plan_file_sha256: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    reason: str
    maximum_pointers: int
    schema_version: int = APPROVAL_SCHEMA_VERSION

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise _error("approval_invalid", "The approval is malformed.")
        try:
            return cls(
                schema_version=int(str(payload["schema_version"])),
                plan_digest=str(payload["plan_digest"]),
                plan_file_sha256=str(payload["plan_file_sha256"]),
                approved_by=str(payload["approved_by"]),
                approved_at=datetime.fromisoformat(
                    str(payload["approved_at"]).replace("Z", "+00:00")
                ),
                expires_at=datetime.fromisoformat(
                    str(payload["expires_at"]).replace("Z", "+00:00")
                ),
                reason=str(payload["reason"]),
                maximum_pointers=int(str(payload["maximum_pointers"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("approval_invalid", "The approval is malformed.") from exc


@dataclass(frozen=True)
class RetireLegacyServiceTeamPointers:
    context: CommandContext
    plan: ServiceTeamPointerRetirementPlan
    approval: ServiceTeamPointerRetirementApproval
    plan_file_sha256: str


@dataclass(frozen=True)
class ServiceTeamPointerRetirementOutcome:
    plan_digest: str
    retired_pointer_count: int


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> ServiceTeamPointerRetirementError:
    return ServiceTeamPointerRetirementError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise _error("invalid_plan", "Timestamps must include a timezone offset.")
    return value.astimezone(UTC)


def _payload_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: str, field: str) -> None:
    cleaned = value.strip()
    if len(cleaned) != 64 or cleaned.lower() != cleaned:
        raise _error("invalid_plan", "A SHA-256 digest is invalid.", field=field)
    try:
        int(cleaned, 16)
    except ValueError as exc:
        raise _error(
            "invalid_plan", "A SHA-256 digest is invalid.", field=field
        ) from exc


def _validate_plan(plan: ServiceTeamPointerRetirementPlan) -> None:
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        raise _error("invalid_plan", "The plan schema version is unsupported.")
    _require_sha256(plan.source_snapshot_sha256, "source_snapshot_sha256")
    _utc(plan.planned_at)
    if not plan.pointers:
        raise _error("invalid_plan", "The plan has no legacy pointers.")
    if len(plan.pointers) > MAX_POINTERS_PER_EXECUTION:
        raise _error("invalid_plan", "The plan exceeds the pointer execution limit.")
    team_ids = {pointer.team_id for pointer in plan.pointers}
    if len(team_ids) != len(plan.pointers):
        raise _error("invalid_plan", "The plan repeats a team identifier.")


def _resolved_person_party(
    *,
    stored_id: UUID,
    parties: dict[UUID, Party],
    users_by_id: dict[UUID, SystemUser],
) -> Party | None:
    party = parties.get(stored_id)
    user = users_by_id.get(stored_id)
    if party is not None and party.party_type == PartyType.person.value:
        if user is not None and user.person_party_id not in {None, stored_id}:
            return None
        return party
    if user is None or user.person_party_id is None:
        return None
    target = parties.get(user.person_party_id)
    if target is None or target.party_type != PartyType.person.value:
        return None
    return target


def _active_principal_for_party(
    *,
    party_id: UUID,
    users_by_party: dict[UUID, tuple[SystemUser, ...]],
) -> bool:
    return sum(user.is_active for user in users_by_party.get(party_id, ())) == 1


def _snapshot(
    db: Session,
) -> tuple[
    list[ServiceTeam],
    list[ServiceTeamMember],
    dict[UUID, Party],
    dict[UUID, SystemUser],
    dict[UUID, tuple[SystemUser, ...]],
]:
    teams = list(db.scalars(select(ServiceTeam).order_by(ServiceTeam.id)).all())
    members = list(
        db.scalars(select(ServiceTeamMember).order_by(ServiceTeamMember.id)).all()
    )
    parties = {row.id: row for row in db.scalars(select(Party)).all()}
    users = list(db.scalars(select(SystemUser)).all())
    users_by_id = {row.id: row for row in users}
    mutable_users_by_party: dict[UUID, list[SystemUser]] = {}
    for user in users:
        if user.person_party_id is not None:
            mutable_users_by_party.setdefault(user.person_party_id, []).append(user)
    users_by_party = {
        party_id: tuple(rows) for party_id, rows in mutable_users_by_party.items()
    }
    return teams, members, parties, users_by_id, users_by_party


def legacy_manager_pointers(
    db: Session,
) -> tuple[LegacyManagerPointer, ...]:
    teams, _members, parties, users_by_id, users_by_party = _snapshot(db)
    result = []
    for team in teams:
        if team.manager_person_id is None:
            continue
        party = _resolved_person_party(
            stored_id=team.manager_person_id,
            parties=parties,
            users_by_id=users_by_id,
        )
        if (
            party is None
            or party.status != PartyIdentityStatus.active.value
            or not _active_principal_for_party(
                party_id=party.id,
                users_by_party=users_by_party,
            )
        ):
            result.append(
                LegacyManagerPointer(
                    team_id=team.id,
                    stored_person_id=team.manager_person_id,
                )
            )
    return tuple(sorted(result, key=lambda row: str(row.team_id)))


def pointer_snapshot_sha256(
    pointers: tuple[LegacyManagerPointer, ...],
) -> str:
    return _payload_digest(
        [
            pointer.payload()
            for pointer in sorted(pointers, key=lambda row: str(row.team_id))
        ]
    )


def audit_service_team_pointer_retirement(
    db: Session,
) -> ServiceTeamPointerRetirementAudit:
    """Return aggregate migration-426 readiness; never reads CRM."""

    teams, members, parties, users_by_id, users_by_party = _snapshot(db)
    legacy_pointers = legacy_manager_pointers(db)
    membership_blockers = 0
    for member in members:
        party = _resolved_person_party(
            stored_id=member.person_id,
            parties=parties,
            users_by_id=users_by_id,
        )
        if party is None:
            ready = False
        elif member.is_active:
            ready = bool(
                party.status == PartyIdentityStatus.active.value
                and _active_principal_for_party(
                    party_id=party.id,
                    users_by_party=users_by_party,
                )
            )
        else:
            ready = True
        if not ready:
            membership_blockers += 1

    legacy_settings = resolve_legacy_service_team_cutover_settings(db)
    known_team_ids = {team.id for team in teams} | {
        item.team_id for item in legacy_settings.teams
    }
    workflow_blockers = legacy_settings.malformed_entry_count
    workflow_blockers += sum(
        item.team_id not in known_team_ids for item in legacy_settings.members
    )
    for item in legacy_settings.members:
        user = users_by_id.get(item.system_user_id)
        party = (
            parties.get(user.person_party_id)
            if user is not None and user.person_party_id is not None
            else None
        )
        if (
            user is None
            or not user.is_active
            or party is None
            or party.party_type != PartyType.person.value
            or party.status != PartyIdentityStatus.active.value
        ):
            workflow_blockers += 1

    duplicate_names = int(
        db.scalar(
            select(func.count()).select_from(
                select(func.lower(ServiceTeam.name))
                .group_by(func.lower(ServiceTeam.name))
                .having(func.count(ServiceTeam.id) > 1)
                .subquery()
            )
        )
        or 0
    )
    return ServiceTeamPointerRetirementAudit(
        team_count=len(teams),
        duplicate_casefolded_team_name_count=duplicate_names,
        manager_reference_count=sum(
            team.manager_person_id is not None for team in teams
        ),
        legacy_manager_pointer_count=len(legacy_pointers),
        membership_count=len(members),
        membership_blocker_count=membership_blockers,
        workflow_setting_blocker_count=workflow_blockers,
    )


def build_pointer_retirement_plan(
    db: Session,
    *,
    planned_at: datetime,
) -> ServiceTeamPointerRetirementPlan:
    pointers = legacy_manager_pointers(db)
    plan = ServiceTeamPointerRetirementPlan(
        source_snapshot_sha256=pointer_snapshot_sha256(pointers),
        planned_at=planned_at,
        pointers=pointers,
    )
    _validate_plan(plan)
    return plan


def _validate_approval(
    command: RetireLegacyServiceTeamPointers,
    *,
    executed_at: datetime,
) -> tuple[str, str]:
    approval = command.approval
    errors: list[str] = []
    if approval.schema_version != APPROVAL_SCHEMA_VERSION:
        errors.append("unsupported approval schema version")
    for value, field in (
        (approval.plan_digest, "approval.plan_digest"),
        (approval.plan_file_sha256, "approval.plan_file_sha256"),
        (command.plan_file_sha256, "plan_file_sha256"),
    ):
        try:
            _require_sha256(value, field)
        except ServiceTeamPointerRetirementError:
            errors.append(f"{field} is invalid")
    approved_by = approval.approved_by.strip()
    reason = approval.reason.strip()
    if not approved_by:
        errors.append("approved_by is required")
    if not reason:
        errors.append("reason is required")
    try:
        approved_at = _utc(approval.approved_at)
        expires_at = _utc(approval.expires_at)
        now = _utc(executed_at)
    except ServiceTeamPointerRetirementError:
        errors.append("approval timestamps must be timezone-aware")
    else:
        if expires_at < approved_at:
            errors.append("approval expires before approval time")
        if expires_at - approved_at > MAX_APPROVAL_WINDOW:
            errors.append("approval window exceeds 24 hours")
        if approved_at > now + MAX_FUTURE_APPROVAL_SKEW:
            errors.append("approval time is in the future")
        if now > expires_at:
            errors.append("approval has expired")
    if approval.plan_digest != command.plan.plan_digest:
        errors.append("approval does not match the plan")
    if approval.plan_file_sha256 != command.plan_file_sha256:
        errors.append("approval does not match the plan file")
    if approval.maximum_pointers < len(command.plan.pointers):
        errors.append("plan exceeds the approved pointer maximum")
    if approval.maximum_pointers > MAX_POINTERS_PER_EXECUTION:
        errors.append("approved pointer maximum exceeds the execution limit")
    if errors:
        raise _error(
            "approval_invalid",
            "The pointer-retirement approval is invalid or stale.",
            errors=tuple(sorted(set(errors))),
        )
    return approved_by, reason


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    actor_type_value, separator, actor_id = str(context.actor or "").partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError:
        return AuditActorType.system, None
    return actor_type, actor_id if separator and actor_id else None


def retire_legacy_service_team_pointers(
    db: Session,
    command: RetireLegacyServiceTeamPointers,
    *,
    executed_at: datetime | None = None,
) -> ServiceTeamPointerRetirementOutcome:
    """Clear only the exact approved unresolved manager pointers."""

    _validate_plan(command.plan)
    approved_by, reason = _validate_approval(
        command,
        executed_at=executed_at or datetime.now(UTC),
    )

    def apply() -> ServiceTeamPointerRetirementOutcome:
        current = legacy_manager_pointers(db)
        if pointer_snapshot_sha256(current) != command.plan.source_snapshot_sha256:
            raise _error(
                "stale_source",
                "The legacy pointer snapshot changed after planning.",
            )
        planned = {
            (pointer.team_id, pointer.stored_person_id)
            for pointer in command.plan.pointers
        }
        if planned != {
            (pointer.team_id, pointer.stored_person_id) for pointer in current
        }:
            raise _error(
                "stale_source",
                "The approved plan does not match every current legacy pointer.",
            )

        actor_type, actor_id = _actor(command.context)
        retired = 0
        for pointer in command.plan.pointers:
            team = db.scalar(
                select(ServiceTeam)
                .where(ServiceTeam.id == pointer.team_id)
                .with_for_update()
            )
            if team is None or team.manager_person_id != pointer.stored_person_id:
                raise _error(
                    "stale_source",
                    "A planned manager pointer changed before execution.",
                    team_id=str(pointer.team_id),
                )
            team.manager_person_id = None
            retired += 1
            evidence = {
                "schema_version": 1,
                "owner": OWNER,
                "team_id": str(team.id),
                "plan_digest": command.plan.plan_digest,
                "source_snapshot_sha256": command.plan.source_snapshot_sha256,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "approved_by_sha256": hashlib.sha256(approved_by.encode()).hexdigest(),
                "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
            }
            stage_audit_event(
                db,
                action="service_team.legacy_manager_pointer_retired",
                entity_type="service_team",
                entity_id=str(team.id),
                actor_type=actor_type,
                actor_id=actor_id,
                metadata=evidence,
            )
            emit_event(
                db,
                EventType.service_team_changed,
                {
                    **evidence,
                    "aggregate_type": "service_team",
                    "aggregate_id": str(team.id),
                    "aggregate_version": str(command.context.command_id),
                    "operation": "legacy_manager_pointer_retired",
                },
                actor=command.context.actor,
            )
        db.flush()
        readiness = audit_service_team_pointer_retirement(db)
        if not readiness.ready:
            raise _error(
                "not_ready",
                "Migration 426 blockers remain after pointer retirement.",
                **readiness.summary(),
            )
        return ServiceTeamPointerRetirementOutcome(
            plan_digest=command.plan.plan_digest,
            retired_pointer_count=retired,
        )

    return execute_owner_command(
        db,
        definition=_RETIRE,
        context=command.context,
        operation=apply,
    )
