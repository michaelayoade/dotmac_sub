"""Cross-domain coordinator for SalesOrder implementation fulfillment.

Each domain owner still writes its own root. This coordinator carries exact
identifiers and commits the combined project/installation handoff once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.project import Project, ProjectType
from app.models.provisioning import ServiceOrder
from app.models.sales import SalesOrder, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.services import installation_projects, projects, settings_spec
from app.services.events import EventType, emit_event


class SalesFulfillmentError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


@dataclass(frozen=True)
class FulfillmentScope:
    sales_order: SalesOrder
    project: Project
    installation_project: InstallationProject


def _project_type(db: Session, order: SalesOrder) -> str:
    candidates = []
    if order.quote is not None and isinstance(order.quote.metadata_, dict):
        candidates.append(order.quote.metadata_.get("project_type"))
        install = order.quote.metadata_.get("install")
        if isinstance(install, dict):
            candidates.append(install.get("project_type"))
    if isinstance(order.metadata_, dict):
        candidates.append(order.metadata_.get("project_type"))
    configured = settings_spec.resolve_value(
        db, SettingDomain.projects, "default_sales_project_type"
    )
    candidates.append(configured)
    allowed = {item.value for item in ProjectType}
    resolved = next(
        (str(value) for value in candidates if str(value or "") in allowed), None
    )
    if resolved is None:
        raise SalesFulfillmentError(
            "project_type_unconfigured",
            "No valid sales implementation project type is configured",
            kind="invalid",
        )
    return resolved


def _customer_address(order: SalesOrder, subscriber: Subscriber) -> str | None:
    if order.quote is not None and isinstance(order.quote.metadata_, dict):
        install = order.quote.metadata_.get("install")
        if isinstance(install, dict):
            value = str(install.get("address") or "").strip()
            if value:
                return value
    parts = [subscriber.address_line1, subscriber.address_line2, subscriber.city]
    return (
        ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        or None
    )


def ensure_implementation_scope(
    db: Session,
    *,
    sales_order_id: UUID,
    actor_id: str,
    commit: bool = True,
) -> FulfillmentScope:
    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesFulfillmentError(
            "actor_required", "Fulfillment actor is required", kind="invalid"
        )
    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == sales_order_id)
        .options(selectinload(SalesOrder.quote), selectinload(SalesOrder.subscriber))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesFulfillmentError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    if order.status == SalesOrderStatus.cancelled.value:
        raise SalesFulfillmentError(
            "sales_order_canceled", "Canceled order cannot create implementation"
        )
    subscriber = order.subscriber
    if subscriber is None:
        raise SalesFulfillmentError(
            "subscriber_not_found", "Sales order Subscriber not found"
        )
    lead_id = order.quote.lead_id if order.quote is not None else None
    try:
        project = projects.prepare_sales_project(
            db,
            sales_order_id=order.id,
            quote_id=order.quote_id,
            subscriber_id=order.subscriber_id,
            lead_id=lead_id,
            name=f"Installation — {order.order_number or order.id}",
            project_type=_project_type(db, order),
            customer_address=_customer_address(order, subscriber),
            region=subscriber.region,
            actor_id=actor,
        )
        installation = installation_projects.ensure_for_project(
            db,
            project_id=project.id,
            subscriber_id=order.subscriber_id,
            actor_id=actor,
        )
        if commit:
            db.commit()
            db.refresh(order)
            db.refresh(project)
            db.refresh(installation)
        return FulfillmentScope(order, project, installation)
    except (
        projects.SalesProjectLifecycleError,
        installation_projects.InstallationScopeError,
    ) as exc:
        if commit:
            db.rollback()
        raise SalesFulfillmentError(
            "fulfillment_rejected", str(exc), kind="invalid"
        ) from exc


def release_verified_implementation(
    db: Session,
    *,
    installation_project_id: UUID,
    verification_event_id: UUID,
    actor_id: str,
    commit: bool = True,
) -> int:
    installation = db.scalars(
        select(InstallationProject)
        .where(InstallationProject.id == installation_project_id)
        .with_for_update()
    ).one_or_none()
    if installation is None:
        raise SalesFulfillmentError(
            "installation_not_found", "Installation project not found", kind="not_found"
        )
    if installation.status != InstallationProjectStatus.verified.value:
        raise SalesFulfillmentError(
            "implementation_not_verified",
            "Only verified implementation can release provisioning",
        )
    project = projects.complete_from_verified_installation(
        db,
        project_id=installation.project_id,
        actor_id=actor_id,
        verification_event_id=verification_event_id,
    )
    from app.services import service_order_lifecycle

    released = 0
    orders = db.scalars(
        select(ServiceOrder)
        .where(ServiceOrder.project_id == project.id)
        .order_by(ServiceOrder.created_at, ServiceOrder.id)
        .with_for_update()
    ).all()
    for order in orders:
        changed = service_order_lifecycle.release_implementation(
            db,
            service_order_id=order.id,
            installation_project_id=installation.id,
            verification_event_id=verification_event_id,
            actor_id=actor_id,
        )
        released += int(changed)
    emit_event(
        db,
        EventType.implementation_released,
        {
            "project_id": str(project.id),
            "installation_project_id": str(installation.id),
            "sales_order_id": str(project.sales_order_id)
            if project.sales_order_id
            else None,
            "verification_event_id": str(verification_event_id),
            "released_service_orders": released,
        },
        actor=actor_id,
        subscriber_id=project.subscriber_id,
    )
    if commit:
        db.commit()
    return released
