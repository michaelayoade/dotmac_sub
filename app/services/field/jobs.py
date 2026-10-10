"""Technician-scoped field job reads over Sub-owned work orders.

CRM can hydrate legacy work-order headers during migration
(``crm_work_order_id`` provenance). Native field execution activity is
authored in sub and recorded on ``work_order`` as sub-authoritative metadata.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.subscriber import Subscriber
from app.models.support import canonical_ticket_status_value
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldAttachmentRead,
    FieldCapabilityAvailability,
    FieldCustomer,
    FieldCustomerExperienceContext,
    FieldEquipmentRead,
    FieldExecutionCapabilities,
    FieldExpenseRequestRead,
    FieldJobDestination,
    FieldJobDetail,
    FieldJobEventRead,
    FieldJobLocation,
    FieldJobSummary,
    FieldMaterialRead,
    FieldMaterialRequestRead,
    FieldMeResponse,
    FieldMovementRead,
    FieldNoteRead,
    FieldProjectContext,
    FieldProjectTaskContext,
    FieldTicketContext,
    FieldWorkLogRead,
)
from app.services.common import apply_pagination
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.field.execution_contracts import (
    FieldAttachmentQuery,
    FieldJobQuery,
    FieldJobsQuery,
    UpdateFieldJobLocation,
)
from app.services.field.map_assets import field_map_assets
from app.services.field.source import mark_sub_authoritative
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActorKind,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
    scoped_work_orders,
)
from app.services.field.work_order_status import (
    FIELD_OPEN_WORK_ORDER_STATUSES,
    WORK_ORDER_TERMINAL_VALUES,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.status_presentation import (
    project_status_presentation,
    project_task_status_presentation,
    ticket_status_presentation,
    work_order_status_presentation,
)

TERMINAL_STATUSES = WORK_ORDER_TERMINAL_VALUES
OPEN_STATUSES = FIELD_OPEN_WORK_ORDER_STATUSES
FieldJobSummaries = list[FieldJobSummary]
FieldJobDestinationPayloads = list[FieldJobDestination]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _technician_name(profile: TechnicianProfile, user: SystemUser | None) -> str:
    if user is not None:
        display_name = (
            user.display_name or f"{user.first_name} {user.last_name}".strip()
        )
        if display_name:
            return display_name
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.crm_person_id or str(profile.person_id)


def _system_user(db: Session, profile: TechnicianProfile) -> SystemUser | None:
    if profile.system_user_id is None:
        return None
    return db.get(SystemUser, profile.system_user_id)


def _profile_from_principal(
    db: Session, principal: dict[str, Any]
) -> TechnicianProfile:
    """Legacy transport adapter for genuinely technician-only collaborators."""
    value = (
        principal.get("principal_id")
        or principal.get("person_id")
        or principal.get("subscriber_id")
    )
    try:
        user_id = UUID(str(value))
    except ValueError as exc:
        raise FieldAccessError(
            code="operations.field_work_order_access.denied",
            message="Active field user not found",
            retryable=False,
        ) from exc
    actor = resolve_field_actor(db, ResolveFieldActor(user_id))
    if actor.kind != FieldActorKind.technician or actor.technician_id is None:
        raise FieldAccessError(
            code="operations.field_work_order_access.denied",
            message="This action requires a technician profile",
            retryable=False,
        )
    profile = db.get(TechnicianProfile, actor.technician_id)
    if profile is None or not profile.is_active:
        raise FieldAccessError(
            code="operations.field_work_order_access.denied",
            message="Technician profile not found",
            retryable=False,
        )
    return profile


def _scoped_query(db: Session, profile: TechnicianProfile):
    """Legacy staff-only scope; vendor membership never expands this query."""
    assignment_ids = select(WorkOrderAssignmentQueue.work_order_mirror_id).filter(
        WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
        WorkOrderAssignmentQueue.assigned_technician_id == profile.id,
    )
    clauses: list[ColumnElement[bool]] = [WorkOrder.id.in_(assignment_ids)]
    if profile.crm_person_id:
        clauses.append(WorkOrder.assigned_to_crm_person_id == profile.crm_person_id)
    return db.query(WorkOrder).filter(WorkOrder.is_active.is_(True), or_(*clauses))


def _subscriber_name(subscriber: Subscriber) -> str | None:
    full = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return (
        subscriber.company_name or full or subscriber.email or subscriber.account_number
    )


def _customer(row: WorkOrder, subscriber: Subscriber | None) -> FieldCustomer | None:
    if subscriber is None:
        return None
    status = getattr(subscriber, "status", None)
    return FieldCustomer(
        subscriber_id=subscriber.id,
        name=_subscriber_name(subscriber),
        phone=subscriber.phone,
        email=subscriber.email,
        address_text=row.address,
        service_plan=getattr(subscriber, "service_plan", None),
        account_number=subscriber.account_number,
        status=(getattr(status, "value", None) or str(status)) if status else None,
    )


def _location(row: WorkOrder) -> FieldJobLocation:
    metadata = row.metadata_ or {}
    latitude = _first_present(metadata, "latitude", "lat")
    longitude = _first_present(metadata, "longitude", "lng")
    source = "cached"
    if isinstance(metadata.get("location"), dict):
        location = metadata["location"]
        latitude = (
            latitude
            if latitude is not None
            else _first_present(location, "latitude", "lat")
        )
        longitude = (
            longitude
            if longitude is not None
            else _first_present(location, "longitude", "lng")
        )
        source = str(location.get("source") or source)
    parsed_latitude = latitude if isinstance(latitude, int | float) else None
    parsed_longitude = longitude if isinstance(longitude, int | float) else None
    return FieldJobLocation(
        latitude=parsed_latitude,
        longitude=parsed_longitude,
        address_text=row.address,
        source=source
        if parsed_latitude is not None and parsed_longitude is not None
        else "address_only",
    )


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


_ASSET_DESTINATION_TYPES = {
    "fdh_cabinet": "cabinet",
    "splice_closure": "closure",
    "fiber_access_point": "fiber_access_point",
    "service_building": "service_building",
    "wireless_mast": "wireless_mast",
}


def _summary(row: WorkOrder) -> FieldJobSummary:
    return FieldJobSummary(
        id=row.public_id,
        work_order_mirror_id=row.id,
        title=row.title,
        description=row.description,
        status=row.status,
        status_presentation=work_order_status_presentation(row.status),
        priority=row.priority,
        work_type=row.work_type,
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        estimated_duration_minutes=row.estimated_duration_minutes,
        estimated_arrival_at=row.estimated_arrival_at,
        started_at=row.started_at,
        paused_at=row.paused_at,
        resumed_at=row.resumed_at,
        completed_at=row.completed_at,
        total_active_seconds=row.total_active_seconds,
        technician_name=row.technician_name or row.assigned_to_name,
        technician_phone=row.technician_phone,
        address=row.address,
        tags=_string_list(row.tags),
    )


def _customer_experience(row: WorkOrder) -> FieldCustomerExperienceContext:
    project = row.project
    project_task = row.project_task
    origin_ticket = row.origin_ticket
    task_ticket = project_task.ticket if project_task is not None else None
    return FieldCustomerExperienceContext(
        project=FieldProjectContext(
            id=project.id,
            number=project.number,
            name=project.name,
            status=project.status,
            status_presentation=project_status_presentation(project.status),
        )
        if project is not None
        else None,
        project_task=FieldProjectTaskContext(
            id=project_task.id,
            number=project_task.number,
            title=project_task.title,
            status=project_task.status,
            status_presentation=project_task_status_presentation(project_task.status),
        )
        if project_task is not None
        else None,
        origin_ticket=FieldTicketContext(
            id=origin_ticket.id,
            number=origin_ticket.number,
            title=origin_ticket.title,
            status=canonical_ticket_status_value(origin_ticket.status),
            status_presentation=ticket_status_presentation(origin_ticket.status),
        )
        if origin_ticket is not None
        else None,
        project_task_ticket=FieldTicketContext(
            id=task_ticket.id,
            number=task_ticket.number,
            title=task_ticket.title,
            status=canonical_ticket_status_value(task_ticket.status),
            status_presentation=ticket_status_presentation(task_ticket.status),
        )
        if task_ticket is not None
        else None,
    )


class FieldJobs:
    @staticmethod
    def me(db: Session, query: ResolveFieldActor) -> FieldMeResponse:
        actor = resolve_field_actor(db, query)
        profile = (
            db.get(TechnicianProfile, actor.technician_id)
            if actor.technician_id
            else None
        )
        user = db.get(SystemUser, actor.system_user_id)
        if user is None:
            raise FieldAccessError(
                code="operations.field_work_order_access.denied",
                message="Active field user not found",
                retryable=False,
            )
        today = datetime.now(UTC).date()
        scoped = scoped_work_orders(db, actor)
        open_jobs = [
            row
            for row in scoped.filter(WorkOrder.status.in_(OPEN_STATUSES)).all()
            if row.scheduled_start is None or row.scheduled_start.date() <= today
        ]
        completed_today = [
            row
            for row in scoped.filter(WorkOrder.status == "completed")
            .filter(WorkOrder.completed_at.isnot(None))
            .all()
            if row.completed_at and row.completed_at.date() == today
        ]
        return FieldMeResponse(
            person_id=actor.person_id,
            name=_technician_name(profile, user)
            if profile is not None
            else (user.display_name or f"{user.first_name} {user.last_name}".strip()),
            email=user.email if user else None,
            technician_title=profile.title if profile else "Vendor",
            region=profile.region if profile else None,
            open_jobs=len(open_jobs),
            completed_today=len(completed_today),
            capabilities=FieldExecutionCapabilities()
            if actor.kind == FieldActorKind.technician
            else FieldExecutionCapabilities(
                attendance=FieldCapabilityAvailability(
                    available=False,
                    reason="Employee attendance is not enabled for vendor accounts.",
                ),
                location_tracking=FieldCapabilityAvailability(
                    available=False,
                    reason="Employee location tracking is not enabled for vendor accounts.",
                ),
                fiber_evidence=FieldCapabilityAvailability(
                    available=False,
                    reason="Fiber evidence requires a configured vendor workflow.",
                ),
                chat=FieldCapabilityAvailability(
                    available=False,
                    reason="Customer chat is not enabled for vendor work orders.",
                ),
                materials=FieldCapabilityAvailability(
                    available=False,
                    reason="Material requests require a configured vendor workflow.",
                ),
                expenses=FieldCapabilityAvailability(
                    available=False,
                    reason="Employee expense claims are not enabled for vendor accounts.",
                ),
                equipment=FieldCapabilityAvailability(
                    available=False,
                    reason="Equipment issue requires a configured vendor workflow.",
                ),
            ),
        )

    @staticmethod
    def list(db: Session, query: FieldJobsQuery) -> FieldJobSummaries:
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        status, date_from, date_to, limit, offset = (
            query.status,
            query.date_from,
            query.date_to,
            query.limit,
            query.offset,
        )
        statement = scoped_work_orders(db, actor)
        if status:
            statement = statement.filter(WorkOrder.status == status)
        if date_from:
            statement = statement.filter(
                or_(
                    WorkOrder.scheduled_start.is_(None),
                    WorkOrder.scheduled_start >= date_from,
                )
            )
        if date_to:
            statement = statement.filter(
                or_(
                    WorkOrder.scheduled_start.is_(None),
                    WorkOrder.scheduled_start <= date_to,
                )
            )
        statement = statement.order_by(
            WorkOrder.scheduled_start.asc().nullslast(),
            WorkOrder.created_at.asc(),
        )
        return [
            _summary(row) for row in apply_pagination(statement, limit, offset).all()
        ]

    @staticmethod
    def get_detail(db: Session, query: FieldJobQuery) -> FieldJobDetail:
        crm_work_order_id = query.public_id
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        principal = {"principal_id": str(actor.system_user_id)}
        row = (
            scoped_work_orders(db, actor)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise FieldAccessError(
                code="operations.field_work_order_access.not_found",
                message="Job not found",
            )
        subscriber = db.get(Subscriber, row.subscriber_id)
        from app.services.field.attachments import field_attachments
        from app.services.field.equipment import field_equipment
        from app.services.field.expense_requests import field_expense_requests
        from app.services.field.material_requests import field_material_requests
        from app.services.field.materials import field_materials
        from app.services.field.movements import list_for_job as list_movements
        from app.services.field.notes import field_notes
        from app.services.field.transitions import field_transitions
        from app.services.field.worklogs import field_worklogs

        materials = (
            field_materials.list_for_job(db, principal, crm_work_order_id)
            if actor.kind == FieldActorKind.technician
            else []
        )
        material_requests = (
            field_material_requests.list_mine(
                db,
                principal,
                crm_work_order_id=crm_work_order_id,
                limit=50,
                offset=0,
            )
            if actor.kind == FieldActorKind.technician
            else []
        )
        expense_requests = (
            field_expense_requests.list_mine(
                db,
                principal,
                crm_work_order_id=crm_work_order_id,
                limit=50,
                offset=0,
            )
            if actor.kind == FieldActorKind.technician
            else []
        )
        notes = field_notes.list_for_job(db, query)
        attachments = field_attachments.list(
            db,
            FieldAttachmentQuery(
                query.requester_system_user_id, public_id=crm_work_order_id
            ),
        )
        worklogs = field_worklogs.list_for_job(db, query)
        events = field_transitions.list_for_job(db, query)
        movements = list_movements(db, row)
        equipment = (
            field_equipment.current_for_job(db, principal, crm_work_order_id)
            if actor.kind == FieldActorKind.technician
            else None
        )

        return FieldJobDetail(
            job=_summary(row),
            completion_requirements=field_transitions.completion_requirements(db),
            customer=_customer(row, subscriber),
            location=_location(row),
            customer_experience=_customer_experience(row),
            access_notes=row.access_notes,
            materials=[FieldMaterialRead.model_validate(item) for item in materials],
            material_requests=[
                FieldMaterialRequestRead.model_validate(item)
                for item in material_requests
            ],
            expense_requests=[
                FieldExpenseRequestRead.model_validate(item)
                for item in expense_requests
            ],
            notes=[FieldNoteRead.model_validate(item) for item in notes],
            attachments=[
                FieldAttachmentRead.model_validate(item) for item in attachments
            ],
            worklogs=[FieldWorkLogRead.model_validate(item) for item in worklogs],
            events=[FieldJobEventRead.model_validate(item) for item in events],
            movements=[FieldMovementRead.model_validate(item) for item in movements],
            equipment=FieldEquipmentRead.model_validate(equipment)
            if equipment is not None
            else None,
            history=[],
        )

    @staticmethod
    def list_destinations(
        db: Session, query: FieldJobQuery
    ) -> FieldJobDestinationPayloads:
        crm_work_order_id = query.public_id
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        principal = {"principal_id": str(actor.system_user_id)}
        row = (
            scoped_work_orders(db, actor)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise FieldAccessError(
                code="operations.field_work_order_access.not_found",
                message="Job not found",
            )

        location = _location(row)
        items: FieldJobDestinationPayloads = [
            FieldJobDestination(
                destination_type="customer",
                destination_id=str(row.subscriber_id) if row.subscriber_id else None,
                label="Infrastructure site"
                if row.work_order_kind == "infrastructure"
                else "Customer site",
                latitude=location.latitude,
                longitude=location.longitude,
                address_text=location.address_text,
            )
        ]

        if location.latitude is not None and location.longitude is not None:
            assets = field_map_assets.nearby(
                db,
                latitude=location.latitude,
                longitude=location.longitude,
                radius_m=750,
                asset_types=builtins.list(_ASSET_DESTINATION_TYPES),
                limit=20,
            )
            for asset in assets:
                items.append(
                    FieldJobDestination(
                        destination_type=_ASSET_DESTINATION_TYPES[asset["type"]],
                        destination_id=str(asset["id"]),
                        label=asset["title"],
                        latitude=asset["latitude"],
                        longitude=asset["longitude"],
                        address_text=asset.get("subtitle"),
                    )
                )

        items.append(
            FieldJobDestination(
                destination_type="other",
                destination_id=None,
                label="Other location",
                latitude=None,
                longitude=None,
                address_text=None,
            )
        )
        return items

    @staticmethod
    def update_location(
        db: Session, command: UpdateFieldJobLocation
    ) -> FieldJobLocation:
        def operation() -> FieldJobLocation:
            db.query(SystemUser).filter(
                SystemUser.id == command.requester_system_user_id
            ).with_for_update().one_or_none()
            actor = resolve_field_actor(
                db, ResolveFieldActor(command.requester_system_user_id)
            )
            row = require_work_order(
                db, FieldWorkOrderScope(actor, command.public_id, lock=True)
            )
            latitude, longitude = command.latitude, command.longitude
            metadata = dict(row.metadata_ or {})
            metadata["location"] = {
                "lat": float(latitude),
                "lng": float(longitude),
                "latitude": float(latitude),
                "longitude": float(longitude),
                "address_text": row.address,
                "source": "manual",
            }
            row.metadata_ = metadata
            mark_sub_authoritative(
                row,
                "location",
                details={"latitude": float(latitude), "longitude": float(longitude)},
            )
            db.flush()
            stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.field_job_location_corrected,
                    producer_owner="operations.field_jobs",
                    source_kind="work_order",
                    source_id=row.id,
                ),
                {
                    "work_order_id": str(row.id),
                    "work_order_public_id": row.public_id,
                    "system_user_id": str(actor.system_user_id),
                },
                context=command.context,
            )
            return _location(row)

        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.field_jobs",
                concern="field job location correction",
                name="update_field_job_location",
            ),
            context=command.context,
            operation=operation,
        )


field_jobs = FieldJobs()
