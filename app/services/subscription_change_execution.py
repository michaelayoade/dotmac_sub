"""Canonical coordinator for deferred service-change execution.

The coordinator owns only the cross-owner invariant and durable links. Money,
invoice settlement, service-order state, field execution, provisioning
readiness, and the final subscription mutation remain with their registered
owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentAllocation
from app.models.catalog import Subscription
from app.models.provisioning import (
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.schemas.billing import InvoiceCreate
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.services import billing as billing_service
from app.services.events import EventType, emit_event
from app.services.subscription_changes import subscription_change_requests
from app.services.work_order_commands import work_order_commands


class SubscriptionChangeExecutionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FulfillmentOutcome:
    request_id: UUID
    service_order_id: UUID
    work_order_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ExecutionDrift:
    request_id: UUID
    code: str
    repairable: bool


def _lock_request(db: Session, request_id: UUID) -> SubscriptionChangeRequest:
    request = db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.id == request_id)
        .with_for_update()
    )
    if request is None or not request.is_active:
        raise SubscriptionChangeExecutionError(
            "service_change_not_found", "Service-change request not found"
        )
    return request


def stage_relocation_charge(
    db: Session, request: SubscriptionChangeRequest
) -> Invoice | None:
    """Create and link the exact relocation invoice inside confirmation."""

    amount = Decimal(request.field_fee_amount or 0)
    if amount <= Decimal("0.00"):
        request.execution_state = SubscriptionChangeExecutionState.payment_settled
        return None
    if request.field_fee_invoice_id is not None:
        return db.get(Invoice, request.field_fee_invoice_id)
    subscription = db.get(Subscription, request.subscription_id)
    if subscription is None:
        raise SubscriptionChangeExecutionError(
            "subscription_not_found", "Subscription not found"
        )
    currency = str(request.field_fee_currency or "").upper()
    if len(currency) != 3:
        raise SubscriptionChangeExecutionError(
            "relocation_currency_missing", "Relocation charge currency is missing"
        )
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=subscription.subscriber_id,
            status=InvoiceStatus.issued,
            currency=currency,
            subtotal=amount,
            total=amount,
            balance_due=amount,
            issued_at=datetime.now(UTC),
            memo=f"Service relocation charge · request {request.id}",
        ),
        commit=False,
    )
    invoice.metadata_ = {
        "payment_flow": "subscription_relocation",
        "subscription_change_request_id": str(request.id),
        "field_quote_fingerprint": request.field_quote_fingerprint,
    }
    request.field_fee_invoice_id = invoice.id
    request.execution_state = SubscriptionChangeExecutionState.awaiting_payment
    db.flush()
    return invoice


def settle_relocation_payment(
    db: Session, *, request_id: UUID, payment_id: UUID
) -> FulfillmentOutcome:
    """Admit canonical allocation evidence and release field fulfillment once."""

    request = _lock_request(db, request_id)
    if request.service_order_id is not None and request.work_order_id is not None:
        return FulfillmentOutcome(
            request.id, request.service_order_id, request.work_order_id, True
        )
    if request.execution_state != SubscriptionChangeExecutionState.awaiting_payment:
        raise SubscriptionChangeExecutionError(
            "service_change_not_awaiting_payment",
            "Service change is not awaiting payment",
        )
    if request.field_fee_invoice_id is None:
        raise SubscriptionChangeExecutionError(
            "relocation_invoice_missing", "Relocation invoice evidence is missing"
        )
    invoice = db.get(Invoice, request.field_fee_invoice_id)
    payment = db.get(Payment, payment_id)
    if invoice is None or payment is None:
        raise SubscriptionChangeExecutionError(
            "settlement_evidence_missing", "Canonical settlement evidence is missing"
        )
    allocated = db.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
            PaymentAllocation.invoice_id == invoice.id,
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.is_active.is_(True),
        )
    )
    expected = Decimal(request.field_fee_amount or 0)
    if (
        invoice.status != InvoiceStatus.paid
        or Decimal(allocated or 0) < expected
        or invoice.currency != request.field_fee_currency
        or Decimal(invoice.total or 0) != expected
    ):
        raise SubscriptionChangeExecutionError(
            "relocation_fee_not_settled",
            "The exact relocation charge has not been canonically settled",
        )
    subscription = db.get(Subscription, request.subscription_id)
    if subscription is None:
        raise SubscriptionChangeExecutionError(
            "subscription_not_found", "Subscription not found"
        )
    request.field_fee_payment_id = payment.id
    request.payment_settled_at = datetime.now(UTC)
    request.execution_state = SubscriptionChangeExecutionState.payment_settled
    service_order = ServiceOrder(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        idempotency_key=f"subscription-change:{request.id}:service-order",
        status=ServiceOrderStatus.submitted,
        order_type=ServiceOrderType.change_service,
        notes="Field relocation issued from canonical service-change intent",
        execution_context={
            "subscription_change_request_id": str(request.id),
            "target_service_address_id": str(request.target_service_address_id),
            "field_fee_invoice_id": str(invoice.id),
            "field_fee_payment_id": str(payment.id),
        },
    )
    db.add(service_order)
    db.flush()
    emit_event(
        db,
        EventType.service_order_created,
        {
            "service_order_id": str(service_order.id),
            "subscription_change_request_id": str(request.id),
            "order_type": ServiceOrderType.change_service.value,
        },
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        service_order_id=service_order.id,
    )
    work_order = work_order_commands.create(
        db,
        WorkOrderHeaderCreate(
            title="Service relocation",
            subscriber_id=subscription.subscriber_id,
            description="Execute the approved service-address relocation.",
            status="scheduled",
            priority="normal",
            work_type="relocation",
            address=f"Address reference {request.target_service_address_id}",
            tags=["service-change", "relocation"],
        ),
        request_id=f"subscription-change:{request.id}:work-order",
        idempotency_key=f"subscription-change:{request.id}:work-order",
        commit=False,
    )
    request.service_order_id = service_order.id
    request.work_order_id = work_order.id
    request.execution_state = SubscriptionChangeExecutionState.fulfillment_released
    db.flush()
    return FulfillmentOutcome(request.id, service_order.id, work_order.id, False)


def finalize_verified_service_change(
    db: Session,
    *,
    request_id: UUID,
    readiness_decision_id: UUID,
    actor_id: str,
) -> SubscriptionChangeRequest:
    """Apply address/offer only from the exact activated readiness decision."""

    request = _lock_request(db, request_id)
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        return request
    decision = db.get(ProvisioningReadinessDecision, readiness_decision_id)
    if (
        decision is None
        or decision.service_order_id != request.service_order_id
        or decision.status != ProvisioningReadinessDecisionStatus.activated
    ):
        raise SubscriptionChangeExecutionError(
            "provisioning_verification_missing",
            "The exact service order has not passed provisioning verification",
        )
    request.provisioning_readiness_decision_id = decision.id
    request.provisioning_verified_at = datetime.now(UTC)
    request.execution_state = SubscriptionChangeExecutionState.provisioning_verified
    if request.status == SubscriptionChangeStatus.pending:
        subscription_change_requests.approve(db, str(request.id), commit=False)
    if request.status != SubscriptionChangeStatus.approved:
        raise SubscriptionChangeExecutionError(
            "service_change_not_finalizable", "Service change cannot be finalized"
        )
    applied = subscription_change_requests.apply(
        db,
        str(request.id),
        plan_change_operation_key=f"subscription-change:{request.id}:finalize",
        plan_change_actor_id=actor_id,
    )
    applied.execution_state = SubscriptionChangeExecutionState.completed
    db.commit()
    db.refresh(applied)
    return applied


def audit_execution_chain(
    db: Session, *, request_id: UUID
) -> tuple[ExecutionDrift, ...]:
    """Report deterministic drift without changing authoritative state."""

    request = db.get(SubscriptionChangeRequest, request_id)
    if request is None:
        return (ExecutionDrift(request_id, "service_change_not_found", False),)
    findings: list[ExecutionDrift] = []
    if (
        request.execution_state == SubscriptionChangeExecutionState.awaiting_payment
        and request.field_fee_invoice_id is not None
    ):
        invoice = db.get(Invoice, request.field_fee_invoice_id)
        if invoice is not None and invoice.status == InvoiceStatus.paid:
            findings.append(ExecutionDrift(request.id, "paid_not_released", True))
    if request.service_order_id is not None and request.execution_state in {
        SubscriptionChangeExecutionState.fulfillment_released,
        SubscriptionChangeExecutionState.provisioning,
    }:
        activated = db.scalar(
            select(ProvisioningReadinessDecision.id).where(
                ProvisioningReadinessDecision.service_order_id
                == request.service_order_id,
                ProvisioningReadinessDecision.status
                == ProvisioningReadinessDecisionStatus.activated,
            )
        )
        if activated is not None:
            findings.append(ExecutionDrift(request.id, "verified_not_finalized", True))
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        subscription = db.get(Subscription, request.subscription_id)
        if subscription is None or (
            subscription.offer_id != request.requested_offer_id
            or (
                request.target_service_address_id is not None
                and subscription.service_address_id != request.target_service_address_id
            )
        ):
            findings.append(
                ExecutionDrift(request.id, "completed_subscription_drift", False)
            )
    return tuple(findings)


def repair_execution_chain(
    db: Session, *, request_id: UUID, actor_id: str
) -> SubscriptionChangeRequest:
    """Idempotently resume a chain from canonical persisted evidence."""

    request = _lock_request(db, request_id)
    if request.execution_state == SubscriptionChangeExecutionState.awaiting_payment:
        allocation = db.scalar(
            select(PaymentAllocation)
            .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
            .where(
                PaymentAllocation.invoice_id == request.field_fee_invoice_id,
                PaymentAllocation.is_active.is_(True),
                Invoice.status == InvoiceStatus.paid,
            )
            .order_by(PaymentAllocation.created_at.asc())
            .limit(1)
        )
        if allocation is None:
            raise SubscriptionChangeExecutionError(
                "relocation_fee_not_settled",
                "No canonical settlement is available for repair",
            )
        settle_relocation_payment(
            db, request_id=request.id, payment_id=allocation.payment_id
        )
        request = _lock_request(db, request.id)
    if request.service_order_id is not None:
        decision = db.scalar(
            select(ProvisioningReadinessDecision)
            .where(
                ProvisioningReadinessDecision.service_order_id
                == request.service_order_id,
                ProvisioningReadinessDecision.status
                == ProvisioningReadinessDecisionStatus.activated,
            )
            .order_by(ProvisioningReadinessDecision.decided_at.desc())
            .limit(1)
        )
        if decision is not None:
            return finalize_verified_service_change(
                db,
                request_id=request.id,
                readiness_decision_id=decision.id,
                actor_id=actor_id,
            )
    return request


__all__ = [
    "FulfillmentOutcome",
    "ExecutionDrift",
    "SubscriptionChangeExecutionError",
    "finalize_verified_service_change",
    "audit_execution_chain",
    "repair_execution_chain",
    "settle_relocation_payment",
    "stage_relocation_charge",
]
