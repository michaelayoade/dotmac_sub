"""Payment provider and event management services."""

from datetime import datetime, timezone

from fastapi import HTTPException
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
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    validate_enum,
)
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.schemas.billing import (
    PaymentProviderCreate,
    PaymentProviderEventIngest,
    PaymentProviderUpdate,
)
from app.services.billing._common import (
    _validate_account,
    _validate_payment_provider,
    _validate_invoice_currency,
    _resolve_collection_account,
    _resolve_payment_channel,
)


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
        invoice = get_by_id(db, Invoice, payload.invoice_id) if payload.invoice_id else None
        if invoice and payload.account_id and str(invoice.account_id) != str(payload.account_id):
            raise HTTPException(status_code=400, detail="Invoice does not belong to account")
        account_id = payload.account_id or (invoice.account_id if invoice else None)
        payment = None
        if payload.payment_id:
            payment = get_by_id(db, Payment, payload.payment_id)
            if not payment:
                raise HTTPException(status_code=404, detail="Payment not found")
        elif payload.external_id:
            payment = (
                db.query(Payment)
                .filter(Payment.external_id == payload.external_id)
                .filter(Payment.provider_id == provider.id)
                .first()
            )
        if not payment and payload.amount and account_id:
            _validate_account(db, str(account_id))
            if invoice:
                _validate_invoice_currency(invoice, payload.currency or invoice.currency)
            channel = _resolve_payment_channel(
                db,
                None,
                None,
                str(provider.id),
            )
            currency = payload.currency or (invoice.currency if invoice else "NGN")
            collection_account = _resolve_collection_account(db, channel, currency, None)
            payment = Payment(
                account_id=account_id,
                amount=payload.amount,
                currency=currency,
                provider_id=provider.id,
                external_id=payload.external_id,
                status=PaymentStatus.pending,
                payment_channel_id=channel.id if channel else None,
                collection_account_id=collection_account.id if collection_account else None,
            )
            db.add(payment)
            db.flush()
            if payload.invoice_id:
                allocation = PaymentAllocation(
                    payment_id=payment.id,
                    invoice_id=payload.invoice_id,
                    amount=payment.amount,
                )
                db.add(allocation)
        elif payment and payload.invoice_id and not payment.allocations:
            allocation = PaymentAllocation(
                payment_id=payment.id,
                invoice_id=payload.invoice_id,
                amount=payment.amount,
            )
            db.add(allocation)
        allocation_invoice_id = (
            payload.invoice_id
            or (str(payment.allocations[0].invoice_id) if payment and payment.allocations else None)
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
        new_status = PaymentProviderEvents._event_status_map.get(payload.event_type)
        if new_status and payment:
            Payments.mark_status(db, str(payment.id), new_status)
            event.status = PaymentProviderEventStatus.processed
            event.processed_at = datetime.now(timezone.utc)
        elif new_status and not payment:
            event.status = PaymentProviderEventStatus.failed
            event.error = "Payment not found for event"
            event.processed_at = datetime.now(timezone.utc)
        else:
            event.status = PaymentProviderEventStatus.processed
            event.processed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(event)
        return event
