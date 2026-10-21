"""Typed, assignment-scoped composition for a technician/vendor job brief.

Work orders own instructions; project tasks own execution tasks; outage
revisions own the exact affected audience. This read creates no business state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.infrastructure import InfrastructureType
from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    NetworkDevice,
    OutageIncidentWorkOrderLink,
    PopSite,
)
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.subscriber import Address, Subscriber
from app.models.work_order import WorkOrder
from app.services.domain_errors import DomainError
from app.services.field.jobs import project_job_location
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActorKind,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.field.work_order_kind import WorkOrderKind
from app.services.infrastructure_catalogue import resolve as resolve_infrastructure
from app.services.projects import engineering_plan_origin
from app.services.topology.outage_audience_read import (
    AudienceScope,
    OutageAudiencePage,
    OutageAudienceQuery,
    read_outage_audience,
)
from app.services.work_order_templates import (
    ClassificationSource,
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateError,
    WorkOrderTemplateQuery,
    WorkOrderTemplateRecommendation,
    recommend_template,
)


class BriefRecommendationStatus(StrEnum):
    available = "available"
    legacy_unclassified = "legacy_unclassified"
    unknown_kind = "unknown_kind"
    unavailable_stale_template = "unavailable_stale_template"
    invalid_classification = "invalid_classification"


class BriefReferenceKind(StrEnum):
    ticket = "ticket"
    project = "project"
    project_task = "project_task"
    outage = "outage"


class DirectionSource(StrEnum):
    work_order = "work_order"
    issued_outage_target = "issued_outage_target"
    project_target = "project_target"


@dataclass(frozen=True, slots=True)
class FieldJobBriefQuery:
    requester_system_user_id: UUID
    public_id: str
    audience_link_id: UUID | None = None
    audience_scope: AudienceScope = AudienceScope.current
    expected_revision_id: UUID | None = None
    after_subscription_id: UUID | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class BriefReference:
    kind: BriefReferenceKind
    id: UUID
    label: str
    number: str | None = None


@dataclass(frozen=True, slots=True)
class BriefDirection:
    label: str
    source: DirectionSource
    entity_id: UUID
    latitude: float | None
    longitude: float | None
    address_text: str | None
    coordinate_provenance: str


@dataclass(frozen=True, slots=True)
class BriefTask:
    id: UUID
    title: str
    instructions: str | None
    status: str
    completed_at: datetime | None
    template_plan_state: str | None


@dataclass(frozen=True, slots=True)
class BriefSignoff:
    evidence_required: bool
    minimum_photo_count: int
    customer_signoff_required: bool
    signature_unavailable_reason_allowed: bool
    requires_as_built_evidence: bool


@dataclass(frozen=True, slots=True)
class AffectedCustomer:
    subscription_id: UUID
    subscriber_id: UUID | None
    name: str | None
    service_address: str | None
    details_available: bool
    details_provenance: str = "current_subscription_record"


@dataclass(frozen=True, slots=True)
class FieldJobBrief:
    public_id: str
    work_order_id: UUID
    issue: str | None
    instructions: str | None
    access_notes: str | None
    references: tuple[BriefReference, ...]
    tasks: tuple[BriefTask, ...]
    signoff: BriefSignoff
    directions: tuple[BriefDirection, ...]
    outage_audience: OutageAudiencePage | None
    affected_customers: tuple[AffectedCustomer, ...]
    # This is the exact subscription audience, not a deduplicated customer
    # count or a claim that every exposed subscription is unavailable.
    affected_subscription_count: int | None

    classification: ExplicitWorkOrderClassification | None = None
    template_recommendation: WorkOrderTemplateRecommendation | None = None
    template_recommendation_status: BriefRecommendationStatus = (
        BriefRecommendationStatus.legacy_unclassified
    )


class FieldJobBriefError(DomainError):
    pass


def _coordinates(
    latitude: float | None, longitude: float | None
) -> tuple[float | None, float | None]:
    if latitude is None or longitude is None:
        return None, None
    if (
        not isfinite(latitude)
        or not isfinite(longitude)
        or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
    ):
        return None, None
    return latitude, longitude


def _affected_customers(
    db: Session, ids: tuple[UUID, ...]
) -> tuple[AffectedCustomer, ...]:
    if not ids:
        return ()
    # Select an explicit operational allowlist. Never hydrate complete customer
    # records (which include billing/KYC) or device records (credentials).
    rows = db.execute(
        select(
            Subscription.id,
            Subscriber.id,
            Subscriber.company_name,
            Subscriber.first_name,
            Subscriber.last_name,
            Address.address_line1,
            Address.address_line2,
            Address.city,
            Address.region,
        )
        .select_from(Subscription)
        .outerjoin(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .outerjoin(
            Address,
            and_(
                Address.id == Subscription.service_address_id,
                Address.subscriber_id == Subscription.subscriber_id,
            ),
        )
        .where(Subscription.id.in_(ids))
    ).all()
    by_id = {}
    for sid, subscriber_id, company, first, last, line1, line2, city, region in rows:
        name = company or " ".join(part for part in (first, last) if part) or None
        address = (
            ", ".join(part for part in (line1, line2, city, region) if part) or None
        )
        by_id[sid] = AffectedCustomer(
            sid, subscriber_id, name, address, subscriber_id is not None
        )
    return tuple(
        by_id.get(sid, AffectedCustomer(sid, None, None, None, False)) for sid in ids
    )


def _target_direction(
    db: Session, audience: OutageAudiencePage
) -> BriefDirection | None:
    scope = audience.issued
    if scope.scope_id is None:
        return None
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    if scope.scope_type in ("fdh-cabinet", "fdh"):
        row = db.execute(
            select(FdhCabinet.name, FdhCabinet.latitude, FdhCabinet.longitude).where(
                FdhCabinet.id == scope.scope_id
            )
        ).one_or_none()
        if row is not None:
            label, latitude, longitude = row
    elif scope.scope_type == "basestation":
        site_row = db.execute(
            select(
                PopSite.name, PopSite.latitude, PopSite.longitude, PopSite.address_line1
            ).where(PopSite.id == scope.scope_id)
        ).one_or_none()
        if site_row is not None:
            label, latitude, longitude, address = site_row
    elif scope.scope_type == "node":
        # A node's site or a nearby pin is not its verified location.
        label = db.scalar(
            select(NetworkDevice.name).where(NetworkDevice.id == scope.scope_id)
        )
    if label is None:
        return None
    latitude, longitude = _coordinates(latitude, longitude)
    return BriefDirection(
        label,
        DirectionSource.issued_outage_target,
        scope.scope_id,
        latitude,
        longitude,
        address,
        "native_plant_record",
    )


def _project_target_direction(
    db: Session, work_order: WorkOrder
) -> BriefDirection | None:
    """Only the native linked project's exact target; never adjacent inventory."""
    if work_order.project_id is None:
        return None
    try:
        origin = engineering_plan_origin(
            db,
            project_id=work_order.project_id,
            task_id=work_order.project_task_id,
            lock=False,
        )
    except DomainError:
        return None
    target = origin.infrastructure
    receipt = db.scalar(
        select(ProjectWorkOrderIssuance).where(
            ProjectWorkOrderIssuance.work_order_id == work_order.id
        )
    )
    if receipt is not None and (
        receipt.project_id != origin.project_id
        or receipt.task_id != origin.task_id
        or receipt.infrastructure_type != target.type.value
        or receipt.infrastructure_id != target.id
    ):
        return None
    record = resolve_infrastructure(db, reference=target)
    if record is None:
        return None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    if target.type is InfrastructureType.cabinet:
        row = db.execute(
            select(FdhCabinet.latitude, FdhCabinet.longitude).where(
                FdhCabinet.id == target.id, FdhCabinet.is_active.is_(True)
            )
        ).one_or_none()
        if row is not None:
            latitude, longitude = row
    elif target.type in {InfrastructureType.location, InfrastructureType.base_station}:
        site = db.execute(
            select(PopSite.latitude, PopSite.longitude, PopSite.address_line1).where(
                PopSite.id == target.id
            )
        ).one_or_none()
        if site is not None:
            latitude, longitude, address = site
    # OLT/PON/device identity alone never authorizes a guessed site pin.
    latitude, longitude = _coordinates(latitude, longitude)
    return BriefDirection(
        record.label,
        DirectionSource.project_target,
        target.id,
        latitude,
        longitude,
        address,
        "native_project_infrastructure",
    )


def _recommendation(
    work_order: WorkOrder,
) -> tuple[
    ExplicitWorkOrderClassification | None,
    WorkOrderTemplateRecommendation | None,
    BriefRecommendationStatus,
]:
    try:
        kind = WorkOrderKind(work_order.work_order_kind)
    except ValueError:
        return None, None, BriefRecommendationStatus.unknown_kind
    values = (
        work_order.purpose,
        work_order.access_technology,
        work_order.classification_source,
        work_order.template_version,
    )
    if all(value is None for value in values):
        return None, None, BriefRecommendationStatus.legacy_unclassified
    if (
        work_order.purpose is None
        or work_order.access_technology is None
        or work_order.classification_source is None
        or work_order.template_version is None
    ):
        return None, None, BriefRecommendationStatus.invalid_classification
    try:
        classification = ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose(work_order.purpose),
            access_technology=WorkOrderAccessTechnology(work_order.access_technology),
            source=ClassificationSource(work_order.classification_source),
        )
        recommendation = recommend_template(
            query=WorkOrderTemplateQuery(kind=kind, classification=classification)
        )
    except (ValueError, WorkOrderTemplateError):
        return None, None, BriefRecommendationStatus.invalid_classification
    if work_order.template_version != recommendation.identity.version:
        return (
            classification,
            None,
            BriefRecommendationStatus.unavailable_stale_template,
        )
    return classification, recommendation, BriefRecommendationStatus.available


def get_job_brief(db: Session, query: FieldJobBriefQuery) -> FieldJobBrief:
    if not 1 <= query.limit <= 100:
        raise FieldJobBriefError(
            code="operations.field_jobs.brief.invalid_page",
            message="Audience page size must be between 1 and 100",
            retryable=False,
        )
    actor = resolve_field_actor(db, ResolveFieldActor(query.requester_system_user_id))
    work_order = require_work_order(
        db,
        FieldWorkOrderScope(
            actor, query.public_id, lock=actor.kind == FieldActorKind.vendor
        ),
    )
    if actor.kind == FieldActorKind.vendor:
        from app.services.work_order_assignment_contracts import (
            CurrentWorkOrderAssignmentQuery,
        )
        from app.services.work_order_commands import work_order_commands

        assignment = work_order_commands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(work_order.id)
        )
        if (
            assignment is None
            or assignment.generation is None
            or assignment.vendor_user_id != actor.vendor_user_id
        ):
            raise FieldAccessError(
                code="operations.field_work_order_access.not_found",
                message="Job not found",
                retryable=False,
            )
    links = (
        db.query(OutageIncidentWorkOrderLink)
        .filter(
            OutageIncidentWorkOrderLink.work_order_id == work_order.id,
        )
        .order_by(OutageIncidentWorkOrderLink.id)
        .all()
    )
    selected = (
        next((link for link in links if link.id == query.audience_link_id), None)
        if query.audience_link_id
        else (links[0] if links else None)
    )
    if query.audience_link_id is not None and selected is None:
        raise FieldJobBriefError(
            code="operations.field_jobs.brief.not_found",
            message="Job outage context not found",
            retryable=False,
        )
    audience = (
        read_outage_audience(
            db,
            OutageAudienceQuery(
                requester_system_user_id=query.requester_system_user_id,
                public_id=query.public_id,
                link_id=selected.id,
                scope=query.audience_scope,
                expected_revision_id=query.expected_revision_id,
                after_subscription_id=query.after_subscription_id,
                limit=query.limit,
            ),
        )
        if selected is not None
        else None
    )
    if audience is None and (
        query.expected_revision_id is not None
        or query.after_subscription_id is not None
    ):
        raise FieldJobBriefError(
            code="operations.field_jobs.brief.not_found",
            message="Job outage context not found",
            retryable=False,
        )
    refs: list[BriefReference] = []
    ticket = work_order.origin_ticket
    if ticket is not None:
        refs.append(
            BriefReference(
                BriefReferenceKind.ticket, ticket.id, ticket.title, ticket.number
            )
        )
    project = work_order.project
    if project is not None:
        refs.append(
            BriefReference(
                BriefReferenceKind.project, project.id, project.name, project.number
            )
        )
    task = work_order.project_task
    tasks: tuple[BriefTask, ...] = ()
    if task is not None and task.is_active:
        refs.append(
            BriefReference(
                BriefReferenceKind.project_task, task.id, task.title, task.number
            )
        )
        tasks = (
            BriefTask(
                task.id,
                task.title,
                task.description,
                task.status,
                task.completed_at,
                task.template_plan_state,
            ),
        )
    refs.extend(
        BriefReference(BriefReferenceKind.outage, link.incident_id, "Linked outage")
        for link in links
    )
    location = project_job_location(work_order)
    latitude, longitude = _coordinates(location.latitude, location.longitude)
    directions = [
        BriefDirection(
            "Job site",
            DirectionSource.work_order,
            work_order.id,
            latitude,
            longitude,
            location.address_text,
            location.source,
        )
    ]
    if audience is not None:
        target = _target_direction(db, audience)
        if target is not None:
            directions.append(target)
    project_target = _project_target_direction(db, work_order)
    if project_target is not None:
        directions.append(project_target)
    requirements = field_transitions.completion_requirements(db)
    selected_revision = (
        (
            audience.issued
            if query.audience_scope == AudienceScope.issued
            else audience.current
        )
        if audience is not None
        else None
    )
    classification, recommendation, recommendation_status = _recommendation(work_order)
    return FieldJobBrief(
        classification=classification,
        template_recommendation=recommendation,
        template_recommendation_status=recommendation_status,
        public_id=work_order.public_id,
        work_order_id=work_order.id,
        issue=ticket.title if ticket is not None else work_order.title,
        instructions=work_order.description,
        access_notes=work_order.access_notes,
        references=tuple(refs),
        tasks=tasks,
        signoff=BriefSignoff(
            requirements.evidence_required,
            requirements.minimum_photo_count,
            requirements.customer_signoff_required,
            requirements.signature_unavailable_reason_allowed,
            work_order.requires_as_built_evidence,
        ),
        directions=tuple(directions),
        outage_audience=audience,
        affected_customers=_affected_customers(db, audience.subscription_ids)
        if audience is not None
        else (),
        affected_subscription_count=selected_revision.subscription_count
        if selected_revision is not None
        else None,
    )
