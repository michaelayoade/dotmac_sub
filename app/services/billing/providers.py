"""Payment provider and event management services."""

import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.schemas.billing import (
    BillingAccountPaymentPreviewRequest,
    PaymentAllocationApply,
    PaymentAllocationConfirm,
    PaymentAllocationCreate,
    PaymentAllocationPreviewRequest,
    PaymentCreate,
    PaymentProviderCreate,
    PaymentProviderEventIngest,
    PaymentProviderUpdate,
)
from app.services.billing._common import (
    _validate_account,
    _validate_invoice_currency,
    _validate_payment_provider,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class PaymentProviders(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentProviderCreate):
        data = payload.model_dump()
        provider = PaymentProvider(**data)
        db.add(provider)
        db.commit()
        db.refresh(provider)
        return provider

    @staticmethod
    def get(db: Session, provider_id: str):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        return provider

    @staticmethod
    def get_by_type(
        db: Session, provider_type: PaymentProviderType
    ) -> PaymentProvider | None:
        """Return the first provider matching the requested provider type."""
        return (
            db.query(PaymentProvider)
            .filter(PaymentProvider.provider_type == provider_type)
            .order_by(PaymentProvider.created_at.asc())
            .first()
        )

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentProvider)
        if is_active is None:
            query = query.filter(PaymentProvider.is_active.is_(True))
        else:
            query = query.filter(PaymentProvider.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentProvider.created_at, "name": PaymentProvider.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentProvider)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentProvider.created_at, "name": PaymentProvider.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, provider_id: str, payload: PaymentProviderUpdate):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(provider, key, value)
        db.commit()
        db.refresh(provider)
        return provider

    @staticmethod
    def delete(db: Session, provider_id: str):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        provider.is_active = False
        db.commit()


class PaymentProviderEvents(ListResponseMixin):
    _event_status_map = {
        "payment.succeeded": PaymentStatus.succeeded,
        "charge.succeeded": PaymentStatus.succeeded,
        # Paystack's actual success event type.
        "charge.success": PaymentStatus.succeeded,
        "payment.failed": PaymentStatus.failed,
        "charge.failed": PaymentStatus.failed,
        "payment.refunded": PaymentStatus.refunded,
        "charge.refunded": PaymentStatus.refunded,
        "payment.reversed": PaymentStatus.reversed,
        "charge.reversed": PaymentStatus.reversed,
        "payment.canceled": PaymentStatus.canceled,
    }
    _event_financial_effect_map = {
        "payment.refunded": PaymentProviderEventFinancialEffect.refund_confirmed,
        "charge.refunded": PaymentProviderEventFinancialEffect.refund_confirmed,
        "payment.reversed": PaymentProviderEventFinancialEffect.reversal_confirmed,
        "charge.reversed": PaymentProviderEventFinancialEffect.reversal_confirmed,
    }

    @staticmethod
    def get(db: Session, event_id: str):
        event = get_by_id(db, PaymentProviderEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Payment event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        provider_id: str | None,
        payment_id: str | None,
        invoice_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentProviderEvent)
        if provider_id:
            query = query.filter(PaymentProviderEvent.provider_id == provider_id)
        if payment_id:
            query = query.filter(PaymentProviderEvent.payment_id == payment_id)
        if invoice_id:
            query = query.filter(PaymentProviderEvent.invoice_id == invoice_id)
        if status:
            query = query.filter(
                PaymentProviderEvent.status
                == validate_enum(status, PaymentProviderEventStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "received_at": PaymentProviderEvent.received_at,
                "processed_at": PaymentProviderEvent.processed_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def ingest(
        db: Session,
        payload: PaymentProviderEventIngest,
        *,
        trusted_financial_effects: bool = False,
    ):
        # Import Payments here to avoid circular dependency at module level
        from app.services.billing.consolidated_payments import (
            ConsolidatedPaymentSettlements,
            consolidated_settlement_key,
        )
        from app.services.billing.payments import (
            PaymentAllocations,
            PaymentReversals,
            Payments,
            Refunds,
        )

        provider = _validate_payment_provider(db, str(payload.provider_id))
        existing_event: PaymentProviderEvent | None = None
        if payload.idempotency_key:
            existing_event = (
                db.query(PaymentProviderEvent)
                .filter(PaymentProviderEvent.provider_id == provider.id)
                .filter(PaymentProviderEvent.idempotency_key == payload.idempotency_key)
                .first()
            )
        invoice = (
            get_by_id(db, Invoice, payload.invoice_id) if payload.invoice_id else None
        )
        if (
            invoice
            and payload.account_id
            and str(invoice.account_id) != str(payload.account_id)
        ):
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to account"
            )
        account_id = payload.account_id or (invoice.account_id if invoice else None)
        # Reseller-consolidated payments carry billing_account_id and no
        # account_id; native subscriber payments carry account_id. When both are
        # present the account-scoped (native) linkage wins.
        billing_account_id = payload.billing_account_id if account_id is None else None
        new_status = payload.status_hint or PaymentProviderEvents._event_status_map.get(
            payload.event_type
        )
        mapped_financial_effect = PaymentProviderEvents._event_financial_effect_map.get(
            payload.event_type
        )
        financial_effect = (
            payload.financial_effect
            or mapped_financial_effect
            or PaymentProviderEventFinancialEffect.none
        )
        if (
            financial_effect != PaymentProviderEventFinancialEffect.none
            and not trusted_financial_effects
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Provider refund/reversal evidence must come through a "
                    "signature-verified provider adapter"
                ),
            )
        if existing_event is not None and not (
            existing_event.payment_id is None
            and new_status == PaymentStatus.succeeded
            and payload.amount is not None
        ):
            return existing_event
        payment = None
        created_settled = False
        if payload.payment_id:
            payment = get_by_id(db, Payment, payload.payment_id)
            if not payment:
                raise HTTPException(status_code=404, detail="Payment not found")
        elif payload.external_id:
            # Also match payments recorded by the synchronous verify path:
            # historical rows were written with provider_id=NULL, and matching
            # them here is what prevents a webhook from double-crediting a
            # transaction the customer already verified.
            payment = (
                db.query(Payment)
                .filter(Payment.external_id == payload.external_id)
                .filter(
                    or_(
                        Payment.provider_id == provider.id,
                        Payment.provider_id.is_(None),
                    )
                )
                .order_by((Payment.provider_id == provider.id).desc())
                .first()
            )
        if not payment and payload.amount and (account_id or billing_account_id):
            if account_id is not None:
                _validate_account(db, str(account_id))
            currency = payload.currency or (invoice.currency if invoice else "NGN")
            if invoice:
                _validate_invoice_currency(invoice, currency)
            if new_status == PaymentStatus.succeeded:
                # Funds confirmed by the provider: settle through the full
                # payment pipeline so webhook-driven settlement is identical to
                # the synchronous verify path — capped allocation, ledger
                # entry, invoice recalculation, service restore, and
                # payment.received events. This is the backstop for customers
                # who paid but never returned to the redirect/verify URL.
                allocations: list[PaymentAllocationApply] | None = None
                if invoice is not None and account_id is not None:
                    balance_due = round_money(to_decimal(invoice.balance_due or 0))
                    if balance_due > Decimal("0.00"):
                        allocations = [
                            PaymentAllocationApply(
                                invoice_id=invoice.id,
                                amount=min(
                                    round_money(to_decimal(payload.amount)),
                                    balance_due,
                                ),
                            )
                        ]
                # Consolidated payments post against the billing account: credit
                # its balance (auto_allocate=False) rather than spreading across
                # member invoices, matching the reseller manual-record flow.
                is_consolidated = billing_account_id is not None and account_id is None
                if is_consolidated:
                    source_id = (
                        payload.idempotency_key
                        or payload.external_id
                        or payload.model_dump_json()
                    )
                    payment = ConsolidatedPaymentSettlements.settle_verified(
                        db,
                        str(billing_account_id),
                        BillingAccountPaymentPreviewRequest(
                            provider_id=provider.id,
                            amount=payload.amount,
                            currency=currency,
                            external_id=payload.external_id,
                            memo=(
                                f"{provider.name} webhook event: {payload.event_type}"
                            ),
                            allocations=None,
                            auto_allocate=False,
                        ),
                        idempotency_key=consolidated_settlement_key(
                            "provider-event", f"{provider.id}:{source_id}"
                        ),
                        origin=PaymentSettlementOrigin.provider_event,
                    ).payment
                else:
                    payment = Payments.create(
                        db,
                        PaymentCreate(
                            account_id=account_id,
                            provider_id=provider.id,
                            amount=payload.amount,
                            currency=currency,
                            status=PaymentStatus.succeeded,
                            external_id=payload.external_id,
                            memo=(
                                f"{provider.name} webhook event: {payload.event_type}"
                            ),
                            allocations=allocations,
                        ),
                        origin=PaymentSettlementOrigin.provider_event,
                    )
                created_settled = True
            else:
                payment = Payments.create(
                    db,
                    PaymentCreate(
                        account_id=account_id,
                        billing_account_id=billing_account_id,
                        amount=payload.amount,
                        currency=currency,
                        provider_id=provider.id,
                        external_id=payload.external_id,
                        status=PaymentStatus.pending,
                        memo=f"{provider.name} observation: {payload.event_type}",
                    ),
                    auto_allocate=False,
                    commit=False,
                    origin=PaymentSettlementOrigin.provider_event,
                )
        elif payment and payload.invoice_id and invoice and not payment.allocations:
            balance_due = round_money(to_decimal(invoice.balance_due or 0))
            alloc_amount = min(round_money(to_decimal(payment.amount)), balance_due)
            if alloc_amount > Decimal("0.00"):
                if payment.status == PaymentStatus.succeeded:
                    preview_request = PaymentAllocationPreviewRequest(
                        payment_id=payment.id,
                        invoice_id=invoice.id,
                        amount=alloc_amount,
                    )
                    preview = PaymentAllocations.preview(db, preview_request)
                    key_material = ":".join(
                        [
                            str(provider.id),
                            payload.idempotency_key or "",
                            payload.external_id or "",
                            payload.event_type,
                            str(invoice.id),
                        ]
                    )
                    key = (
                        "provider-allocation-"
                        + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                    )
                    PaymentAllocations.confirm(
                        db,
                        PaymentAllocationConfirm(
                            payment_id=payment.id,
                            invoice_id=invoice.id,
                            amount=alloc_amount,
                            preview_fingerprint=preview.fingerprint,
                            idempotency_key=key,
                        ),
                        commit=False,
                    )
                else:
                    PaymentAllocations.record_intent(
                        db,
                        PaymentAllocationCreate(
                            payment_id=payment.id,
                            invoice_id=invoice.id,
                            amount=alloc_amount,
                            memo=f"{provider.name} invoice intent",
                        ),
                        commit=False,
                    )
        allocation_invoice_id: UUID | None = payload.invoice_id
        if allocation_invoice_id is None and payment and payment.allocations:
            allocation_invoice_id = payment.allocations[0].invoice_id
        if existing_event is not None:
            # Legacy dead-letter replay created the idempotency event without
            # settlement fields, leaving payment_id NULL. The idempotency row
            # proves receipt, not completion: resume it through the payment
            # owner and attach the result instead of returning early forever.
            event = existing_event
            event.payment_id = payment.id if payment else None
            event.invoice_id = allocation_invoice_id
            event.event_type = payload.event_type
            event.external_id = payload.external_id
            event.payload = payload.payload
            event.amount = payload.amount
            event.currency = payload.currency
            event.financial_effect = financial_effect
            event.error = None
        else:
            event = PaymentProviderEvent(
                provider_id=provider.id,
                payment_id=payment.id if payment else None,
                invoice_id=allocation_invoice_id,
                event_type=payload.event_type,
                external_id=payload.external_id,
                idempotency_key=payload.idempotency_key,
                amount=payload.amount,
                currency=payload.currency,
                financial_effect=financial_effect,
                payload=payload.payload,
            )
            db.add(event)
        db.flush()
        if created_settled:
            # Payments.create already ran the full success pipeline; calling
            # mark_status again would just re-stamp paid_at and re-run recalc.
            event.status = PaymentProviderEventStatus.processed
            event.processed_at = datetime.now(UTC)
        elif new_status and payment:
            if new_status == PaymentStatus.refunded:
                Refunds.process_provider_event_refund(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                    commit=False,
                )
            elif new_status == PaymentStatus.reversed:
                PaymentReversals.process_provider_event_reversal(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                    commit=False,
                )
            elif (
                new_status == PaymentStatus.succeeded
                and payment.billing_account_id is not None
            ):
                ConsolidatedPaymentSettlements.settle_verified(
                    db,
                    str(payment.billing_account_id),
                    BillingAccountPaymentPreviewRequest(
                        amount=payment.amount,
                        currency=payment.currency,
                        paid_at=payment.paid_at,
                        memo=payment.memo,
                        payment_method_id=payment.payment_method_id,
                        payment_channel_id=payment.payment_channel_id,
                        collection_account_id=payment.collection_account_id,
                        provider_id=payment.provider_id,
                        external_id=payment.external_id,
                        allocations=[
                            PaymentAllocationApply(
                                invoice_id=allocation.invoice_id,
                                amount=allocation.amount,
                                memo=allocation.memo,
                            )
                            for allocation in payment.allocations
                            if allocation.is_active
                        ]
                        or None,
                        auto_allocate=payment.auto_allocate_on_settlement,
                    ),
                    idempotency_key=consolidated_settlement_key(
                        "provider-event", str(event.id)
                    ),
                    origin=PaymentSettlementOrigin.provider_event,
                    commit=False,
                    existing_payment_id=str(payment.id),
                )
            else:
                Payments.mark_status(
                    db,
                    str(payment.id),
                    new_status,
                    origin=PaymentSettlementOrigin.provider_event,
                )
            event.status = PaymentProviderEventStatus.processed
            event.processed_at = datetime.now(UTC)
        elif new_status and not payment:
            event.status = PaymentProviderEventStatus.failed
            event.error = "Payment not found for event"
            event.processed_at = datetime.now(UTC)
        else:
            event.status = PaymentProviderEventStatus.processed
            event.processed_at = datetime.now(UTC)
        db.commit()
        db.refresh(event)
        return event
