from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, PaymentStatus
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.billing import PaymentAllocationApply, PaymentCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import suspend_subscription


def _make_subscriber(db: Session, *, status: SubscriberStatus) -> Subscriber:
    subscriber = Subscriber(
        first_name="Payment",
        last_name="Status",
        email=f"payment-status-{uuid.uuid4().hex[:8]}@example.com",
        status=status,
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _make_offer(db: Session) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Payment Status Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _make_subscription(
    db: Session,
    subscriber: Subscriber,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=_make_offer(db).id,
        status=status,
        billing_mode=BillingMode.postpaid,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _make_overdue_invoice(
    db: Session,
    subscriber: Subscriber,
    *,
    total: str = "1000.00",
) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        due_at=datetime.now(UTC) - timedelta(days=3),
    )
    db.add(invoice)
    db.flush()
    return invoice


def test_final_invoice_payment_restores_subscription_and_recomputes_account_status(
    db_session,
):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    subscription = _make_subscription(db_session, subscriber)
    invoice = _make_overdue_invoice(db_session, subscriber)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        emit=False,
    )
    subscriber.status = SubscriberStatus.suspended
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.paid
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active


def test_partial_invoice_payment_does_not_restore_or_mark_account_active(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    subscription = _make_subscription(db_session, subscriber)
    invoice = _make_overdue_invoice(db_session, subscriber)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        emit=False,
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("400.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("400.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.partially_paid
    assert subscription.status == SubscriptionStatus.suspended
    assert subscriber.status == SubscriberStatus.suspended


def test_payment_recomputes_stale_account_status_after_subscription_already_active(
    db_session,
):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.suspended)
    subscription = _make_subscription(
        db_session, subscriber, status=SubscriptionStatus.active
    )
    invoice = _make_overdue_invoice(db_session, subscriber)
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.paid
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active
