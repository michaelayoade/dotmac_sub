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
from app.models.catalog import AccessCredential, OfferRadiusProfile, Subscription
from app.models.provisioning import (
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.models.radius import RadiusUser
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.schemas.billing import InvoiceCreate
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.services import billing as billing_service
from app.services.events import EventType, emit_event
from app.services.radius_access_state import stage_subscription_radius_profile
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
class RemoteReprovisionOutcome:
    request_id: UUID
    radius_profile_id: UUID
    radius_user_id: UUID | None
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


def stage_remote_reprovision(
    db: Session, request: SubscriptionChangeRequest
) -> RemoteReprovisionOutcome:
    """Stage the exact offer profile on one subscription-scoped credential.

    The live offer remains unchanged. A later verifier must observe the exact
    profile on the exact RADIUS user after this request watermark.
    """

    if request.remote_radius_profile_id is not None:
        return RemoteReprovisionOutcome(
            request.id,
            request.remote_radius_profile_id,
            request.remote_radius_user_id,
            True,
        )
    profiles = list(
        db.scalars(
            select(OfferRadiusProfile).where(
                OfferRadiusProfile.offer_id == request.requested_offer_id
            )
        ).all()
    )
    if len(profiles) != 1:
        raise SubscriptionChangeExecutionError(
            "remote_radius_profile_ambiguous",
            "The requested offer must have exactly one RADIUS profile",
        )
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscription_id == request.subscription_id,
                AccessCredential.is_active.is_(True),
            )
        ).all()
    )
    if len(credentials) != 1:
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "Remote reprovisioning requires exactly one active subscription credential",
        )
    credential = credentials[0]
    radius_user = db.scalar(
        select(RadiusUser).where(RadiusUser.access_credential_id == credential.id)
    )
    requested_at = datetime.now(UTC)
    stage_subscription_radius_profile(
        db,
        subscription_id=request.subscription_id,
        credential_id=credential.id,
        radius_profile_id=profiles[0].profile_id,
    )
    request.remote_radius_profile_id = profiles[0].profile_id
    request.remote_radius_user_id = radius_user.id if radius_user is not None else None
    request.remote_reprovision_requested_at = requested_at
    request.execution_state = SubscriptionChangeExecutionState.provisioning
    db.flush()
    return RemoteReprovisionOutcome(
        request.id,
        profiles[0].profile_id,
        radius_user.id if radius_user is not None else None,
        False,
    )


def finalize_verified_remote_reprovision(
    db: Session, *, request_id: UUID, actor_id: str
) -> SubscriptionChangeRequest:
    """Apply a remote change only from exact, fresh RADIUS read-model evidence."""

    request = _lock_request(db, request_id)
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        return request
    if (
        request.execution_state != SubscriptionChangeExecutionState.provisioning
        or request.remote_radius_profile_id is None
        or request.remote_reprovision_requested_at is None
    ):
        raise SubscriptionChangeExecutionError(
            "remote_reprovision_not_staged",
            "Remote reprovisioning has not been staged",
        )
    radius_user = (
        db.get(RadiusUser, request.remote_radius_user_id)
        if request.remote_radius_user_id is not None
        else db.scalar(
            select(RadiusUser).where(
                RadiusUser.subscription_id == request.subscription_id
            )
        )
    )
    observed_at = radius_user.last_sync_at if radius_user is not None else None
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    requested_at = request.remote_reprovision_requested_at
    if requested_at is not None and requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    if (
        radius_user is None
        or radius_user.subscription_id != request.subscription_id
        or radius_user.radius_profile_id != request.remote_radius_profile_id
        or observed_at is None
        or requested_at is None
        or observed_at < requested_at
    ):
        raise SubscriptionChangeExecutionError(
            "remote_reprovision_verification_missing",
            "The exact target RADIUS profile has not been observed after staging",
        )
    request.remote_radius_user_id = radius_user.id
    request.provisioning_verified_at = observed_at
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
        plan_change_operation_key=f"subscription-change:{request.id}:remote-finalize",
        plan_change_preview_fingerprint=request.confirmation_preview_fingerprint,
        plan_change_effective_at=_confirmation_effective_at(request),
        plan_change_actor_id=actor_id,
    )
    applied.execution_state = SubscriptionChangeExecutionState.completed
    db.commit()
    db.refresh(applied)
    return applied


def _confirmation_effective_at(request: SubscriptionChangeRequest) -> datetime | None:
    snapshot = request.confirmation_snapshot or {}
    raw = snapshot.get("preview_effective_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
        request.execution_state == SubscriptionChangeExecutionState.provisioning
        and _delivery_mode(request) == "remote_reprovision"
        and _remote_radius_verification_ready(db, request)
    ):
        findings.append(
            ExecutionDrift(request.id, "remote_verified_not_finalized", True)
        )
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
    if (
        request.execution_state == SubscriptionChangeExecutionState.provisioning
        and _delivery_mode(request) == "remote_reprovision"
    ):
        return finalize_verified_remote_reprovision(
            db, request_id=request.id, actor_id=actor_id
        )
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


def _delivery_mode(request: SubscriptionChangeRequest) -> str | None:
    snapshot = request.confirmation_snapshot or {}
    value = snapshot.get("delivery_mode")
    return value if isinstance(value, str) else None


def _remote_radius_verification_ready(
    db: Session, request: SubscriptionChangeRequest
) -> bool:
    if (
        request.remote_radius_profile_id is None
        or request.remote_reprovision_requested_at is None
    ):
        return False
    users = list(
        db.scalars(
            select(RadiusUser).where(
                RadiusUser.subscription_id == request.subscription_id,
                RadiusUser.radius_profile_id == request.remote_radius_profile_id,
            )
        ).all()
    )
    if len(users) != 1 or users[0].last_sync_at is None:
        return False
    observed_at = users[0].last_sync_at
    requested_at = request.remote_reprovision_requested_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    return observed_at >= requested_at


__all__ = [
    "FulfillmentOutcome",
    "ExecutionDrift",
    "SubscriptionChangeExecutionError",
    "RemoteReprovisionOutcome",
    "finalize_verified_remote_reprovision",
    "finalize_verified_service_change",
    "audit_execution_chain",
    "repair_execution_chain",
    "settle_relocation_payment",
    "stage_relocation_charge",
    "stage_remote_reprovision",
]
