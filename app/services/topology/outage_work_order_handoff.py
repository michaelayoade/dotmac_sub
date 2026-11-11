"""Issue one field work order for a shared infrastructure outage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
)
from app.models.rbac import Role, SystemUserRole
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscriber import UserType
from app.models.support import Ticket, TicketChannel, TicketStatus
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.network import (
    InfrastructureWorkOrderHeaderCreate,
    InfrastructureWorkOrderIssueRequest,
)
from app.schemas.support import TicketCreate
from app.services import service_team_composition
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.infrastructure_ticket_selection import InfrastructureKind
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topology.outage import (
    infrastructure_link_for,
    latest_scope_revision,
    link_infrastructure_ticket,
)
from app.services.ui_contracts import Action

IssueErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]
ISSUE_SCOPE = "network.outage_work_order:issue"
TICKET_CREATE_SCOPE = "network.outage_ticket:create"
REQUIRED_PERMISSIONS = frozenset({"monitoring:write", "operations:dispatch:write"})
TICKET_CREATE_PERMISSIONS = frozenset({"monitoring:write", "support:ticket:create"})
_DEFINITION = OwnerCommandDefinition(
    owner="network.outage_work_order_handoff",
    concern="infrastructure-ticket work-order issuance eligibility",
    name="issue_infrastructure_work_order",
)
_TICKET_DEFINITION = OwnerCommandDefinition(
    owner="network.outage_work_order_handoff",
    concern="shared-outage infrastructure ticket issuance",
    name="create_infrastructure_ticket",
)
_ISSUABLE_STATUSES = frozenset({"open", "confirmed"})


class HandoffActorType(StrEnum):
    SYSTEM_USER = "system_user"
    API_KEY = "api_key"
    SERVICE = "service"


@dataclass(frozen=True)
class OutageWorkOrderIssueCommand:
    incident_id: UUID
    request: InfrastructureWorkOrderIssueRequest
    actor_id: UUID
    actor_type: HandoffActorType
    permissions: frozenset[str]
    context: CommandContext
    request_id: str | None = None
    expected_ticket_id: UUID | None = None


@dataclass(frozen=True)
class InfrastructureTicketWorkOrderIssueCommand:
    ticket_id: UUID
    request: InfrastructureWorkOrderIssueRequest
    actor_id: UUID
    actor_type: HandoffActorType
    permissions: frozenset[str]
    context: CommandContext
    request_id: str | None = None
    manual_target_name: str | None = None


@dataclass(frozen=True)
class InfrastructureTicketWorkOrderSurface:
    target_label: str
    action: Action
    expected_scope_revision_sequence: int | None = None
    manual_target_name: str | None = None


@dataclass(frozen=True)
class OutageInfrastructureTicketIssueCommand:
    incident_id: UUID
    actor_id: UUID
    permissions: frozenset[str]
    context: CommandContext
    request_id: str | None = None
    ticket_payload: TicketCreate | None = None
    source: str = "outage_console"


@dataclass(frozen=True)
class InfrastructureAssetTicketIssueCommand:
    ticket_type: str
    asset_kind: InfrastructureKind
    asset_id: UUID
    ticket_payload: TicketCreate
    actor_id: UUID
    permissions: frozenset[str]
    context: CommandContext


def create_ticket_for_infrastructure_asset(
    db: Session, command: InfrastructureAssetTicketIssueCommand
) -> OutageInfrastructureTicketIssueResult:
    """Declare or reuse an asset outage and issue its one ticket atomically."""
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_TICKET_DEFINITION,
        context=command.context,
        operation=lambda: _create_ticket_for_infrastructure_asset(db, command),
    )


def _create_ticket_for_infrastructure_asset(
    db: Session, command: InfrastructureAssetTicketIssueCommand
) -> OutageInfrastructureTicketIssueResult:
    from sqlalchemy import select

    from app.models.network import FdhCabinet
    from app.models.network_monitoring import (
        NetworkDevice,
        PopSite,
    )
    from app.schemas.infrastructure import InfrastructureReference
    from app.services import infrastructure_catalogue
    from app.services.infrastructure_ticket_selection import (
        allowed_kinds,
        catalogue_type,
    )
    from app.services.topology.outage import declare_outage

    if command.context.scope != TICKET_CREATE_SCOPE:
        raise OutageWorkOrderHandoffError(
            "invalid_command_scope",
            "Infrastructure ticket scope is invalid",
            kind="forbidden",
        )
    if TICKET_CREATE_PERMISSIONS - command.permissions:
        raise OutageWorkOrderHandoffError(
            "permission_required",
            "Monitoring and ticket create permissions are required",
            kind="forbidden",
        )
    kind = command.asset_kind
    if (
        kind not in allowed_kinds(db, command.ticket_type)
        or kind is InfrastructureKind.manual
    ):
        raise OutageWorkOrderHandoffError(
            "invalid_asset_kind",
            "Ticket type does not support this outage target",
            kind="invalid",
        )
    if command.ticket_payload.ticket_type != command.ticket_type or any(
        (
            command.ticket_payload.subscriber_id,
            command.ticket_payload.customer_account_id,
            command.ticket_payload.customer_person_id,
            command.ticket_payload.lead_id,
            command.ticket_payload.related_outage_ticket_id,
        )
    ):
        raise OutageWorkOrderHandoffError(
            "customerless_ticket_required",
            "Infrastructure tickets cannot target one customer",
            kind="invalid",
        )
    if not (command.ticket_payload.description or "").strip():
        raise OutageWorkOrderHandoffError(
            "description_required", "Description is required", kind="invalid"
        )
    inventory_type = catalogue_type(db, kind, command.ticket_type)
    if inventory_type is None:
        infrastructure_catalogue.lock_network_node(
            db,
            reference=infrastructure_catalogue.NetworkNodeReference(command.asset_id),
        )
    else:
        infrastructure_catalogue.lock_reference(
            db,
            reference=InfrastructureReference(type=inventory_type, id=command.asset_id),
        )
    basestation = (
        db.get(PopSite, command.asset_id)
        if kind is InfrastructureKind.basestation
        else None
    )
    cabinet = (
        db.get(FdhCabinet, command.asset_id)
        if kind is InfrastructureKind.cabinet
        else None
    )
    node = (
        db.get(NetworkDevice, command.asset_id)
        if kind is InfrastructureKind.node
        else None
    )
    if not any((basestation, cabinet, node)):
        raise OutageWorkOrderHandoffError(
            "asset_not_found", "Infrastructure asset is unavailable", kind="not_found"
        )
    target_column = {
        InfrastructureKind.basestation: OutageIncident.basestation_id,
        InfrastructureKind.cabinet: OutageIncident.fdh_cabinet_id,
        InfrastructureKind.node: OutageIncident.root_node_id,
    }[kind]
    incident = db.scalar(
        select(OutageIncident)
        .where(
            target_column == command.asset_id,
            OutageIncident.status.in_(tuple(_ISSUABLE_STATUSES)),
        )
        .order_by(OutageIncident.started_at.desc())
        .limit(1)
        .with_for_update()
    )
    if incident is None:
        incident = declare_outage(
            db,
            basestation=basestation,
            fdh=cabinet,
            node=node,
            declared_by=str(command.actor_id),
            note=command.ticket_payload.description,
        )
    return _create_infrastructure_ticket(
        db,
        OutageInfrastructureTicketIssueCommand(
            incident_id=incident.id,
            actor_id=command.actor_id,
            permissions=command.permissions,
            context=command.context,
            ticket_payload=command.ticket_payload,
            source="support_ticket_form",
        ),
    )


class OutageWorkOrderHandoffError(DomainError):
    def __init__(self, code: str, message: str, *, kind: IssueErrorKind = "conflict"):
        super().__init__(code=code, message=message, details={"kind": kind})
        self.kind = kind


@dataclass(frozen=True)
class OutageWorkOrderIssueResult:
    work_order: WorkOrder
    link: OutageIncidentWorkOrderLink | None
    replayed: bool


@dataclass(frozen=True)
class OutageInfrastructureTicketIssueResult:
    ticket: Ticket
    link: OutageIncidentTicketLink
    replayed: bool


def _validate_issuer(db: Session, ticket: Ticket, *, actor_id: object) -> UUID:
    try:
        actor_uuid = coerce_uuid(actor_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise OutageWorkOrderHandoffError(
            "staff_required",
            "An active staff member must issue field work",
            kind="forbidden",
        ) from exc
    if actor_uuid is None or ticket.service_team_id is None:
        raise OutageWorkOrderHandoffError(
            "team_required",
            "Assign an active service team before issuing field work",
            kind="forbidden",
        )
    team = db.get(ServiceTeam, ticket.service_team_id)
    actor = db.get(SystemUser, actor_uuid)
    if team is None or not team.is_active:
        raise OutageWorkOrderHandoffError(
            "team_inactive", "The assigned service team is not active", kind="invalid"
        )
    if (
        actor is None
        or not actor.is_active
        or actor.user_type is not UserType.system_user
    ):
        raise OutageWorkOrderHandoffError(
            "staff_required",
            "An active staff member must issue field work",
            kind="forbidden",
        )
    member = db.scalar(
        select(ServiceTeamMember.id).where(
            ServiceTeamMember.team_id == team.id,
            ServiceTeamMember.person_id == actor.person_party_id,
            ServiceTeamMember.is_active.is_(True),
        )
    )
    admin = db.scalar(
        select(SystemUserRole.id)
        .join(Role, Role.id == SystemUserRole.role_id)
        .where(
            SystemUserRole.system_user_id == actor_uuid,
            SystemUserRole.scope_type == "",
            SystemUserRole.scope_id == "",
            Role.is_active.is_(True),
            func.lower(Role.name) == "admin",
        )
    )
    if member is None and admin is None:
        raise OutageWorkOrderHandoffError(
            "assigned_team_membership_required",
            "Only an active team member or administrator may issue field work",
            kind="forbidden",
        )
    return actor_uuid


def _validate_incident(
    db: Session, incident: OutageIncident, *, actor_id: object
) -> tuple[Ticket, OutageScopeRevision, UUID]:
    if incident.status not in _ISSUABLE_STATUSES:
        raise OutageWorkOrderHandoffError(
            "incident_not_issuable",
            "Only open or confirmed outages can receive field work",
            kind="invalid",
        )
    if not any(
        (incident.basestation_id, incident.fdh_cabinet_id, incident.root_node_id)
    ):
        raise OutageWorkOrderHandoffError(
            "incident_target_required",
            "The outage must have a resolved infrastructure target",
            kind="invalid",
        )
    link = infrastructure_link_for(db, incident.id)
    if link is None:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_required",
            "Link the canonical infrastructure ticket before issuing field work",
            kind="invalid",
        )
    ticket = db.get(Ticket, link.ticket_id)
    if ticket is None or not ticket.is_active:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_missing",
            "The canonical infrastructure ticket is unavailable",
            kind="conflict",
        )
    if ticket.merged_into_ticket_id or str(ticket.status) in {
        "TicketStatus.closed",
        "TicketStatus.canceled",
        "closed",
        "canceled",
        "merged",
    }:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_terminal",
            "The canonical infrastructure ticket is already closed",
            kind="invalid",
        )
    revision = latest_scope_revision(db, incident.id)
    if revision is None:
        raise OutageWorkOrderHandoffError(
            "scope_revision_required",
            "The outage audience must be calculated before field work is issued",
            kind="conflict",
        )
    return ticket, revision, _validate_issuer(db, ticket, actor_id=actor_id)


def issue_action(
    db: Session, incident: OutageIncident, *, actor_id: object | None
) -> Action:
    try:
        _validate_incident(db, incident, actor_id=actor_id)
    except OutageWorkOrderHandoffError as exc:
        return Action(
            key="issue_infrastructure_work_order",
            label="Create infrastructure work order",
            allowed=False,
            reason=exc.message,
            permission="operations:dispatch:write",
        )
    return Action(
        key="issue_infrastructure_work_order",
        label="Create infrastructure work order",
        allowed=True,
        permission="operations:dispatch:write",
    )


def ticket_work_order_surface(
    db: Session, ticket: Ticket, *, actor_id: object | None
) -> InfrastructureTicketWorkOrderSurface | None:
    """Project the ticket's durable infrastructure origin and issue readiness."""
    links = (
        db.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.ticket_id == ticket.id,
            OutageIncidentTicketLink.role == "infrastructure",
        )
        .all()
    )
    if links:
        incident = (
            db.get(OutageIncident, links[0].incident_id) if len(links) == 1 else None
        )
        if incident is None:
            return InfrastructureTicketWorkOrderSurface(
                target_label=ticket.title,
                action=Action(
                    key="issue_infrastructure_work_order",
                    label="Create infrastructure work order",
                    allowed=False,
                    reason="The linked outage is unavailable or ambiguous",
                    permission="operations:dispatch:write",
                ),
            )
        return InfrastructureTicketWorkOrderSurface(
            target_label=_incident_target_label(db, incident),
            action=issue_action(db, incident, actor_id=actor_id),
            expected_scope_revision_sequence=(
                revision.sequence
                if (revision := latest_scope_revision(db, incident.id)) is not None
                else None
            ),
        )
    target_label = _manual_infrastructure_target(db, ticket)
    if target_label is None:
        return None
    try:
        _validate_manual_ticket(db, ticket, actor_id=actor_id)
    except OutageWorkOrderHandoffError as exc:
        action = Action(
            key="issue_infrastructure_work_order",
            label="Create infrastructure work order",
            allowed=False,
            reason=exc.message,
            permission="operations:dispatch:write",
        )
    else:
        action = Action(
            key="issue_infrastructure_work_order",
            label="Create infrastructure work order",
            allowed=True,
            permission="operations:dispatch:write",
        )
    return InfrastructureTicketWorkOrderSurface(
        target_label=target_label or "Confirm the affected infrastructure",
        action=action,
        manual_target_name=target_label,
    )


def _manual_infrastructure_target(db: Session, ticket: Ticket) -> str | None:
    from app.services.infrastructure_ticket_selection import allowed_kinds

    if any(
        (
            ticket.subscriber_id,
            ticket.customer_account_id,
            ticket.customer_person_id,
            ticket.lead_id,
        )
    ):
        return None
    metadata = ticket.metadata_ or {}
    selection = metadata.get("infrastructure_selection")
    if isinstance(selection, dict) and selection.get("mode") == "manual":
        return str(selection.get("name") or "").strip()
    if InfrastructureKind.manual not in allowed_kinds(db, ticket.ticket_type):
        return None
    return str(metadata.get("base_station_details") or "").strip()


def _validate_manual_ticket(
    db: Session, ticket: Ticket, *, actor_id: object | None
) -> UUID:
    if (
        not ticket.is_active
        or ticket.merged_into_ticket_id
        or str(ticket.status)
        in {
            "closed",
            "canceled",
            "merged",
            "TicketStatus.closed",
            "TicketStatus.canceled",
        }
    ):
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_terminal",
            "Only an active ticket can issue field work",
            kind="invalid",
        )
    if any(
        (
            ticket.subscriber_id,
            ticket.customer_account_id,
            ticket.customer_person_id,
            ticket.lead_id,
        )
    ):
        raise OutageWorkOrderHandoffError(
            "customerless_ticket_required",
            "Infrastructure work orders require a customerless ticket",
            kind="invalid",
        )
    return _validate_issuer(db, ticket, actor_id=actor_id)


def issue_from_ticket(
    db: Session, command: InfrastructureTicketWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=lambda: _issue_from_ticket(db, command),
    )


def _issue_from_ticket(
    db: Session, command: InfrastructureTicketWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    if (
        command.context.scope != ISSUE_SCOPE
        or (REQUIRED_PERMISSIONS | {"support:ticket:update"}) - command.permissions
    ):
        raise OutageWorkOrderHandoffError(
            "permission_required",
            "Ticket update, monitoring, and dispatch write permissions are required",
            kind="forbidden",
        )
    if not str(command.context.idempotency_key or "").strip():
        raise OutageWorkOrderHandoffError(
            "idempotency_key_required", "Idempotency-Key is required", kind="invalid"
        )
    links = (
        db.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.ticket_id == command.ticket_id,
            OutageIncidentTicketLink.role == "infrastructure",
        )
        .all()
    )
    if links:
        if len(links) != 1:
            raise OutageWorkOrderHandoffError(
                "ambiguous_outage_link", "Ticket has multiple infrastructure outages"
            )
        return _issue_work_order(
            db,
            OutageWorkOrderIssueCommand(
                incident_id=links[0].incident_id,
                request=command.request,
                actor_id=command.actor_id,
                actor_type=command.actor_type,
                permissions=command.permissions,
                context=command.context,
                request_id=command.request_id,
                expected_ticket_id=command.ticket_id,
            ),
        )
    from app.services.work_order_assignment_contracts import WorkOrderLinkageFence
    from app.services.work_order_commands import lock_work_order_linkage

    lock_work_order_linkage(
        db, query=WorkOrderLinkageFence(ticket_id=command.ticket_id)
    )
    ticket = (
        db.query(Ticket)
        .populate_existing()
        .filter(Ticket.id == command.ticket_id)
        .with_for_update()
        .one_or_none()
    )
    if ticket is None:
        raise OutageWorkOrderHandoffError(
            "ticket_not_found", "Ticket not found", kind="not_found"
        )
    if (
        db.query(OutageIncidentTicketLink.id)
        .filter(
            OutageIncidentTicketLink.ticket_id == ticket.id,
            OutageIncidentTicketLink.role == "infrastructure",
        )
        .first()
    ):
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_changed",
            "The ticket was linked to an outage; reload before issuing field work",
            kind="conflict",
        )
    if _manual_infrastructure_target(db, ticket) is None:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_required",
            "Ticket is not an infrastructure ticket",
            kind="invalid",
        )
    actor_uuid = _validate_manual_ticket(db, ticket, actor_id=command.actor_id)
    target_name = str(command.manual_target_name or "").strip()
    if not 3 <= len(target_name) <= 255:
        raise OutageWorkOrderHandoffError(
            "manual_target_required",
            "Enter the affected infrastructure name (3 to 255 characters)",
            kind="invalid",
        )
    existing_ids = {
        row.id
        for row in db.query(WorkOrder.id).filter(
            WorkOrder.origin_ticket_id == ticket.id
        )
    }
    payload = command.request
    description = f"Issuance reason: {payload.reason}"
    if payload.description:
        description += f"\n\n{payload.description}"
    from app.services import work_order_commands

    command_key = f"ticket-infrastructure-work-order:{ticket.id}:{command.context.idempotency_key}"
    stable_request_id = (
        f"infra-ticket-wo-{hashlib.sha256(command_key.encode()).hexdigest()[:24]}"
    )
    work_order = (
        work_order_commands.work_order_commands.stage_infrastructure_work_order(
            db,
            InfrastructureWorkOrderHeaderCreate(
                title=payload.title,
                description=description,
                status=payload.status,
                priority=payload.priority,
                work_type=payload.work_type,
                address=payload.address,
                scheduled_start=payload.scheduled_start,
                scheduled_end=payload.scheduled_end,
                estimated_duration_minutes=payload.estimated_duration_minutes,
                required_skills=payload.required_skills,
                tags=payload.tags,
                access_notes=payload.access_notes,
                requires_as_built_evidence=payload.requires_as_built_evidence,
            ),
            origin_ticket_id=ticket.id,
            infrastructure_target_name=target_name,
            auth={
                "principal_type": command.actor_type.value,
                "principal_id": str(actor_uuid),
            },
            request_id=stable_request_id,
            idempotency_key=command_key,
        )
    )
    from app.models.audit import AuditEvent

    existing_audit = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.action == "infrastructure_ticket.work_order_issued",
            AuditEvent.entity_type == "support_ticket",
            AuditEvent.entity_id == str(ticket.id),
            AuditEvent.request_id == stable_request_id,
        )
        .one_or_none()
    )
    if existing_audit is None:
        stage_audit_event(
            db,
            action="infrastructure_ticket.work_order_issued",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor=AuditActor(actor_type=AuditActorType.user, actor_id=str(actor_uuid)),
            request_id=stable_request_id,
            metadata={
                "owner": "network.outage_work_order_handoff",
                "work_order_id": work_order.public_id,
                "target_name": target_name,
                "assigned_team_id": str(ticket.service_team_id),
                "reason": payload.reason,
                "transport_request_id": command.request_id,
            },
        )
        emit_event(
            db,
            EventType.infrastructure_ticket_work_order_issued,
            {
                "ticket_id": str(ticket.id),
                "work_order_id": str(work_order.id),
                "work_order_public_id": work_order.public_id,
                "target_name": target_name,
                "assigned_team_id": str(ticket.service_team_id),
                "issued_by": str(actor_uuid),
            },
            actor=str(actor_uuid),
        )
    return OutageWorkOrderIssueResult(
        work_order=work_order, link=None, replayed=work_order.id in existing_ids
    )


def list_for_incident(db: Session, incident_id: object) -> list[WorkOrder]:
    return (
        db.query(WorkOrder)
        .join(
            OutageIncidentWorkOrderLink,
            OutageIncidentWorkOrderLink.work_order_id == WorkOrder.id,
        )
        .filter(OutageIncidentWorkOrderLink.incident_id == coerce_uuid(incident_id))
        .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
        .all()
    )


def issue_work_order(
    db: Session, command: OutageWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=lambda: _issue_work_order(db, command),
    )


def _issue_work_order(
    db: Session, command: OutageWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    if command.context.scope != ISSUE_SCOPE:
        raise OutageWorkOrderHandoffError(
            "invalid_command_scope",
            "Infrastructure work-order scope is invalid",
            kind="forbidden",
        )
    missing = REQUIRED_PERMISSIONS - command.permissions
    if missing:
        raise OutageWorkOrderHandoffError(
            "permission_required",
            "Monitoring and dispatch write permissions are required",
            kind="forbidden",
        )
    key = str(command.context.idempotency_key or "").strip()
    if not key:
        raise OutageWorkOrderHandoffError(
            "idempotency_key_required", "Idempotency-Key is required", kind="invalid"
        )
    incident = (
        db.query(OutageIncident)
        .filter(OutageIncident.id == command.incident_id)
        .with_for_update()
        .one_or_none()
    )
    if incident is None:
        raise OutageWorkOrderHandoffError(
            "incident_not_found", "Outage not found", kind="not_found"
        )
    ticket, revision, actor_uuid = _validate_incident(
        db, incident, actor_id=command.actor_id
    )
    if (
        command.expected_ticket_id is not None
        and ticket.id != command.expected_ticket_id
    ):
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_changed",
            "The outage ticket changed; reload the ticket",
            kind="conflict",
        )
    payload = command.request
    if (
        payload.expected_scope_revision_sequence is not None
        and payload.expected_scope_revision_sequence != revision.sequence
    ):
        raise OutageWorkOrderHandoffError(
            "scope_revision_stale",
            "The outage audience changed; refresh the page and try again",
            kind="conflict",
        )
    target_type = (
        "basestation"
        if incident.basestation_id
        else (
            "fdh-cabinet"
            if incident.fdh_cabinet_id
            else "node"
            if incident.root_node_id
            else "unresolved"
        )
    )
    target_id = (
        incident.basestation_id or incident.fdh_cabinet_id or incident.root_node_id
    )
    command_key = f"outage-work-order:{incident.id}:{key}"
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "incident_id": str(incident.id),
                "revision": revision.sequence,
                **payload.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    existing = (
        db.query(OutageIncidentWorkOrderLink)
        .filter(
            OutageIncidentWorkOrderLink.incident_id == incident.id,
            OutageIncidentWorkOrderLink.idempotency_key == command_key,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.command_fingerprint != fingerprint:
            raise OutageWorkOrderHandoffError(
                "idempotency_conflict",
                "This work-order request key was already used",
                kind="conflict",
            )
        work_order = db.get(WorkOrder, existing.work_order_id)
        if work_order is None:
            raise OutageWorkOrderHandoffError(
                "linked_work_order_missing",
                "The linked work order is missing",
                kind="conflict",
            )
        return OutageWorkOrderIssueResult(
            work_order=work_order, link=existing, replayed=True
        )

    description = f"Issuance reason: {payload.reason}"
    if payload.description:
        description += f"\n\n{payload.description}"
    from app.services import work_order_commands

    work_order = (
        work_order_commands.work_order_commands.stage_infrastructure_work_order(
            db,
            InfrastructureWorkOrderHeaderCreate(
                title=payload.title,
                description=description,
                status=payload.status,
                priority=payload.priority,
                work_type=payload.work_type,
                address=payload.address,
                scheduled_start=payload.scheduled_start,
                scheduled_end=payload.scheduled_end,
                estimated_duration_minutes=payload.estimated_duration_minutes,
                required_skills=payload.required_skills,
                tags=payload.tags,
                access_notes=payload.access_notes,
                requires_as_built_evidence=payload.requires_as_built_evidence,
            ),
            origin_ticket_id=ticket.id,
            auth={
                "principal_type": command.actor_type.value,
                "principal_id": str(actor_uuid),
            },
            request_id=command.request_id or str(command.context.command_id),
            idempotency_key=command_key,
        )
    )
    link = OutageIncidentWorkOrderLink(
        incident_id=incident.id,
        work_order_id=work_order.id,
        scope_revision_id=revision.id,
        scope_revision_sequence=revision.sequence,
        membership_token=revision.membership_token,
        target_type=target_type,
        target_id=target_id,
        idempotency_key=command_key,
        command_fingerprint=fingerprint,
        created_by=str(actor_uuid),
    )
    db.add(link)
    stage_audit_event(
        db,
        action="outage.infrastructure_work_order_issued",
        entity_type="outage_incident",
        entity_id=str(incident.id),
        actor=AuditActor(actor_type=AuditActorType.user, actor_id=str(actor_uuid)),
        request_id=command.request_id or str(command.context.command_id),
        metadata={
            "owner": "network.outage_work_order_handoff",
            "work_order_id": work_order.public_id,
            "ticket_id": str(ticket.id),
            "scope_revision_sequence": revision.sequence,
            "membership_token": revision.membership_token,
            "target_type": target_type,
        },
    )
    emit_event(
        db,
        EventType.outage_infrastructure_work_order_issued,
        {
            "incident_id": str(incident.id),
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
            "ticket_id": str(ticket.id),
            "scope_revision_sequence": revision.sequence,
            "membership_token": revision.membership_token,
        },
        actor=str(actor_uuid),
    )
    db.flush()
    return OutageWorkOrderIssueResult(work_order=work_order, link=link, replayed=False)


def ticket_issue_action(db: Session, incident: OutageIncident) -> Action:
    """Project readiness for creating the one canonical outage ticket."""
    if infrastructure_link_for(db, incident.id) is not None:
        return Action(
            key="create_infrastructure_ticket",
            label="Create infrastructure ticket",
            allowed=False,
            reason="This outage already has a canonical infrastructure ticket",
            permission="support:ticket:create",
        )
    if incident.status not in _ISSUABLE_STATUSES:
        return Action(
            key="create_infrastructure_ticket",
            label="Create infrastructure ticket",
            allowed=False,
            reason="Only open or confirmed outages can receive an infrastructure ticket",
            permission="support:ticket:create",
        )
    try:
        routing = service_team_composition.resolve_routing_team(
            db, domain="network.outage", route_key="incident.primary"
        )
    except Exception as exc:  # routing owner maps ambiguity to a safe UI reason
        return Action(
            key="create_infrastructure_ticket",
            label="Create infrastructure ticket",
            allowed=False,
            reason=str(exc),
            permission="support:ticket:create",
        )
    return Action(
        key="create_infrastructure_ticket",
        label="Create infrastructure ticket",
        allowed=routing is not None,
        reason=(
            None
            if routing is not None
            else "No active outage-response team is configured"
        ),
        permission="support:ticket:create",
    )


def create_infrastructure_ticket(
    db: Session, command: OutageInfrastructureTicketIssueCommand
) -> OutageInfrastructureTicketIssueResult:
    """Create and bind the canonical ticket for one shared outage atomically."""
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_TICKET_DEFINITION,
        context=command.context,
        operation=lambda: _create_infrastructure_ticket(db, command),
    )


def _create_infrastructure_ticket(
    db: Session, command: OutageInfrastructureTicketIssueCommand
) -> OutageInfrastructureTicketIssueResult:
    if command.context.scope != TICKET_CREATE_SCOPE:
        raise OutageWorkOrderHandoffError(
            "invalid_command_scope",
            "Infrastructure ticket scope is invalid",
            kind="forbidden",
        )
    missing = TICKET_CREATE_PERMISSIONS - command.permissions
    if missing:
        raise OutageWorkOrderHandoffError(
            "permission_required",
            "Monitoring and support ticket create permissions are required",
            kind="forbidden",
        )
    if not str(command.context.idempotency_key or "").strip():
        raise OutageWorkOrderHandoffError(
            "idempotency_key_required", "Idempotency-Key is required", kind="invalid"
        )
    incident = (
        db.query(OutageIncident)
        .filter(OutageIncident.id == command.incident_id)
        .with_for_update()
        .one_or_none()
    )
    if incident is None:
        raise OutageWorkOrderHandoffError(
            "incident_not_found", "Outage not found", kind="not_found"
        )
    existing = infrastructure_link_for(db, incident.id)
    if existing is not None:
        ticket = db.get(Ticket, existing.ticket_id)
        if ticket is None:
            raise OutageWorkOrderHandoffError(
                "infrastructure_ticket_missing",
                "The outage link points to a missing ticket",
                kind="conflict",
            )
        if (
            command.ticket_payload is not None
            and ticket.ticket_type != command.ticket_payload.ticket_type
        ):
            raise OutageWorkOrderHandoffError(
                "infrastructure_ticket_type_conflict",
                "The active outage already has a ticket with a different type",
                kind="conflict",
            )
        if (
            command.ticket_payload is not None
            and command.ticket_payload.service_team_id is not None
            and ticket.service_team_id != command.ticket_payload.service_team_id
        ):
            raise OutageWorkOrderHandoffError(
                "infrastructure_ticket_team_conflict",
                "The active outage already has a ticket assigned to another service team",
                kind="conflict",
            )
        return OutageInfrastructureTicketIssueResult(
            ticket=ticket, link=existing, replayed=True
        )
    if incident.status not in _ISSUABLE_STATUSES:
        raise OutageWorkOrderHandoffError(
            "incident_not_issuable",
            "Only open or confirmed outages can receive an infrastructure ticket",
            kind="invalid",
        )
    requested_team_id = (
        command.ticket_payload.service_team_id
        if command.ticket_payload is not None
        else None
    )
    if requested_team_id is not None:
        requested_team = db.get(ServiceTeam, requested_team_id)
        if requested_team is None or not requested_team.is_active:
            raise OutageWorkOrderHandoffError(
                "selected_team_inactive",
                "Select an active service team for the infrastructure ticket",
                kind="invalid",
            )
        team_id = requested_team.id
    else:
        routing = service_team_composition.resolve_routing_team(
            db, domain="network.outage", route_key="incident.primary"
        )
        if routing is None:
            raise OutageWorkOrderHandoffError(
                "outage_team_missing",
                "No active outage-response team is configured",
                kind="conflict",
            )
        team_id = routing.team_id
    target = _incident_target_label(db, incident)
    from app.services import support as support_service

    ticket_payload = (
        command.ticket_payload.model_copy(deep=True)
        if command.ticket_payload
        else TicketCreate(
            title=f"Infrastructure outage — {target}"[:255],
            description=incident.note or f"Shared outage affecting {target}.",
            ticket_type="outage",
            priority="urgent",
            channel=TicketChannel.web,
            status=TicketStatus.open,
            service_team_id=team_id,
            tags=["infrastructure", "shared-outage"],
            metadata_={
                "outage_incident_id": str(incident.id),
                "ticket_role": "infrastructure",
            },
        )
    )
    ticket_payload.service_team_id = team_id
    ticket_payload.status = TicketStatus.open
    ticket_payload.tags = list(
        dict.fromkeys([*ticket_payload.tags, "infrastructure", "shared-outage"])
    )
    ticket_payload.metadata_ = {
        **(ticket_payload.metadata_ or {}),
        "outage_incident_id": str(incident.id),
        "ticket_role": "infrastructure",
    }
    if command.ticket_payload is not None:
        ticket_payload.metadata_["base_station_details"] = target
    ticket = support_service.Tickets.create(
        db,
        ticket_payload,
        actor_id=str(command.actor_id),
        routing_mode=support_service.TicketCreationRoutingMode.preserve_requested_team,
    )
    link = link_infrastructure_ticket(
        db,
        incident,
        ticket.id,
        linked_by=str(command.actor_id),
        source=command.source,
    )
    stage_audit_event(
        db,
        action="outage.infrastructure_ticket_created",
        entity_type="outage_incident",
        entity_id=str(incident.id),
        actor=AuditActor(
            actor_type=AuditActorType.user, actor_id=str(command.actor_id)
        ),
        request_id=str(command.context.command_id),
        metadata={
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "service_team_id": str(team_id),
        },
    )
    return OutageInfrastructureTicketIssueResult(
        ticket=ticket, link=link, replayed=False
    )


def _incident_target_label(db: Session, incident: OutageIncident) -> str:
    from app.models.network import FdhCabinet
    from app.models.network_monitoring import NetworkDevice, PopSite

    if incident.basestation_id is not None:
        pop = db.get(PopSite, incident.basestation_id)
        return f"BTS: {pop.name}" if pop is not None else "Basestation"
    if incident.fdh_cabinet_id is not None:
        fdh = db.get(FdhCabinet, incident.fdh_cabinet_id)
        return f"FDH: {fdh.code or fdh.name}" if fdh is not None else "FDH cabinet"
    if incident.root_node_id is not None:
        node = db.get(NetworkDevice, incident.root_node_id)
        return f"Node: {node.name}" if node is not None else "Network node"
    return "Unresolved network target"
