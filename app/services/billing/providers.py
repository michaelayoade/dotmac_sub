"""Payment provider and event management services."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.billing import (
    PaymentAllocationApply,
    PaymentCreate,
    PaymentProviderCreate,
    PaymentProviderEventIngest,
    PaymentProviderUpdate,
)
from app.services import settings_spec
from app.services.billing._common import (
    _resolve_collection_account,
    _resolve_payment_channel,
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
        fields_set = payload.model_fields_set
        if "provider_type" not in fields_set:
            default_provider_type = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_payment_provider_type"
            )
            if default_provider_type:
                data["provider_type"] = validate_enum(
                    default_provider_type, PaymentProviderType, "provider_type"
                )
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
        "payment.canceled": PaymentStatus.canceled,
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
    def ingest(db: Session, payload: PaymentProviderEventIngest):
        # Import Payments here to avoid circular dependency at module level
        from app.services.billing.payments import Payments

        provider = _validate_payment_provider(db, str(payload.provider_id))
        if payload.idempotency_key:
            existing = (
                db.query(PaymentProviderEvent)
                .filter(PaymentProviderEvent.provider_id == provider.id)
                .filter(PaymentProviderEvent.idempotency_key == payload.idempotency_key)
                .first()
            )
            if existing:
                return existing
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
                payment = Payments.create(
                    db,
                    PaymentCreate(
                        account_id=account_id,
                        billing_account_id=billing_account_id,
                        provider_id=provider.id,
                        amount=payload.amount,
                        currency=currency,
                        status=PaymentStatus.succeeded,
                        external_id=payload.external_id,
                        memo=f"{provider.name} webhook event: {payload.event_type}",
                        allocations=allocations,
                    ),
                    auto_allocate=not is_consolidated,
                )
                created_settled = True
            else:
                channel = _resolve_payment_channel(
                    db,
                    None,
                    None,
                    str(provider.id),
                )
                collection_account = _resolve_collection_account(
                    db, channel, currency, None
                )
                payment = Payment(
                    account_id=account_id,
                    billing_account_id=billing_account_id,
                    amount=payload.amount,
                    currency=currency,
                    provider_id=provider.id,
                    external_id=payload.external_id,
                    status=PaymentStatus.pending,
                    payment_channel_id=channel.id if channel else None,
                    collection_account_id=collection_account.id
                    if collection_account
                    else None,
                )
                db.add(payment)
                db.flush()
                if payload.invoice_id and invoice:
                    allocation = PaymentAllocation(
                        payment_id=payment.id,
                        invoice_id=payload.invoice_id,
                        amount=payment.amount,
                    )
                    db.add(allocation)
                    db.flush()
                    from app.services.billing.payments import (
                        _create_payment_ledger_entry,
                    )

                    _create_payment_ledger_entry(db, payment, invoice, payment.amount)
        elif payment and payload.invoice_id and invoice and not payment.allocations:
            balance_due = round_money(to_decimal(invoice.balance_due or 0))
            alloc_amount = min(round_money(to_decimal(payment.amount)), balance_due)
            if alloc_amount > Decimal("0.00"):
                allocation = PaymentAllocation(
                    payment_id=payment.id,
                    invoice_id=payload.invoice_id,
                    amount=alloc_amount,
                )
                db.add(allocation)
                db.flush()
                from app.services.billing.payments import _create_payment_ledger_entry

                _create_payment_ledger_entry(db, payment, invoice, alloc_amount)
        allocation_invoice_id = payload.invoice_id or (
            str(payment.allocations[0].invoice_id)
            if payment and payment.allocations
            else None
        )
        event = PaymentProviderEvent(
            provider_id=provider.id,
            payment_id=payment.id if payment else None,
            invoice_id=allocation_invoice_id,
            event_type=payload.event_type,
            external_id=payload.external_id,
            idempotency_key=payload.idempotency_key,
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
            Payments.mark_status(db, str(payment.id), new_status)
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
