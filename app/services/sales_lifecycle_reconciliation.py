"""Idempotent repair coordinator for sales-to-service lifecycle projections."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_experience import CustomerExperienceHandoff
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.sales import SalesOrder, SalesOrderStatus
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
)
from app.services import customer_experience_handoffs, sales_fulfillment


class SalesLifecycleReconciliationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.kind = "conflict"


def reconcile_sales_to_service_lifecycle(
    db: Session,
    *,
    apply: bool = False,
    actor_id: str = "sales.lifecycle_reconciler",
) -> dict[str, int | bool]:
    """Report drift and optionally request repair through canonical owners."""

    counts: Counter[str] = Counter()
    orders = list(
        db.scalars(
            select(SalesOrder)
            .where(
                SalesOrder.is_active.is_(True),
                SalesOrder.status != SalesOrderStatus.cancelled.value,
            )
            .order_by(SalesOrder.created_at, SalesOrder.id)
        ).all()
    )
    for order in orders:
        if order.project is not None:
            continue
        counts["missing_implementation_scope"] += 1
        if apply:
            try:
                sales_fulfillment.ensure_implementation_scope(
                    db,
                    sales_order_id=order.id,
                    actor_id=actor_id,
                    commit=False,
                )
            except ValueError as exc:
                db.rollback()
                raise SalesLifecycleReconciliationError(
                    "implementation_scope_repair_rejected", str(exc)
                ) from exc
            counts["implementation_scope_repaired"] += 1

    verified = list(
        db.scalars(
            select(InstallationProject)
            .where(
                InstallationProject.status == InstallationProjectStatus.verified.value
            )
            .order_by(InstallationProject.created_at, InstallationProject.id)
        ).all()
    )
    for installation in verified:
        unreleased = list(
            db.scalars(
                select(ServiceOrder).where(
                    ServiceOrder.installation_project_id == installation.id,
                    ServiceOrder.implementation_verification_event_id.is_(None),
                )
            ).all()
        )
        if not unreleased:
            continue
        counts["verified_implementation_not_released"] += len(unreleased)
        evidence = db.scalars(
            select(InstallationProjectLifecycleEvent)
            .where(
                InstallationProjectLifecycleEvent.project_id == installation.id,
                InstallationProjectLifecycleEvent.to_status
                == InstallationProjectStatus.verified.value,
            )
            .order_by(
                InstallationProjectLifecycleEvent.occurred_at.desc(),
                InstallationProjectLifecycleEvent.id.desc(),
            )
            .limit(1)
        ).one_or_none()
        if evidence is None:
            counts["verified_implementation_missing_evidence"] += len(unreleased)
        elif apply:
            try:
                released = sales_fulfillment.release_verified_implementation(
                    db,
                    installation_project_id=installation.id,
                    verification_event_id=evidence.event_id,
                    actor_id=actor_id,
                    commit=False,
                )
            except ValueError as exc:
                db.rollback()
                raise SalesLifecycleReconciliationError(
                    "verified_release_repair_rejected", str(exc)
                ) from exc
            counts["service_orders_released"] += released

    active_orders = list(
        db.scalars(
            select(ServiceOrder).where(
                ServiceOrder.sales_order_line_id.is_not(None),
                ServiceOrder.status == ServiceOrderStatus.active,
            )
        ).all()
    )
    for service_order in active_orders:
        existing = db.scalars(
            select(CustomerExperienceHandoff).where(
                CustomerExperienceHandoff.service_order_id == service_order.id
            )
        ).one_or_none()
        if existing is not None:
            continue
        counts["active_service_orders_without_cx_handoff"] += 1
        if apply:
            try:
                customer_experience_handoffs.ensure_ready_for_service_order(
                    db, service_order_id=service_order.id, actor_id=actor_id
                )
            except ValueError as exc:
                db.rollback()
                raise SalesLifecycleReconciliationError(
                    "cx_handoff_repair_rejected", str(exc)
                ) from exc
            counts["cx_handoffs_repaired"] += 1

    if apply:
        db.commit()
    else:
        db.rollback()
    return {
        "apply": apply,
        "missing_implementation_scope": counts["missing_implementation_scope"],
        "implementation_scope_repaired": counts["implementation_scope_repaired"],
        "verified_implementation_not_released": counts[
            "verified_implementation_not_released"
        ],
        "verified_implementation_missing_evidence": counts[
            "verified_implementation_missing_evidence"
        ],
        "service_orders_released": counts["service_orders_released"],
        "active_service_orders_without_cx_handoff": counts[
            "active_service_orders_without_cx_handoff"
        ],
        "cx_handoffs_repaired": counts["cx_handoffs_repaired"],
    }
