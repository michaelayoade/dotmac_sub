"""Non-payment settlement (write-off / void) of an overdue invoice must lift the
overdue enforcement lock and restore service — same as the restore-on-payment
path. Without this, the debt clears but the service stays suspended on a stale
overdue lock.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.billing import InvoiceBulkVoidRequest, InvoiceCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import has_active_lock, suspend_subscription


def _overdue_postpaid(db, subscriber, offer):
    """A postpaid sub suspended by an overdue lock for a real past-due invoice."""
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
    )
    db.add(sub)
    db.commit()
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
        ),
    )
    suspend_subscription(
        db,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
    )
    db.commit()
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended
    return sub, invoice


def test_write_off_overdue_invoice_restores_service(
    db_session, subscriber, catalog_offer
):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.write_off(db_session, str(invoice.id))

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(sub.id), reason=EnforcementReason.overdue
    )


def test_void_overdue_invoice_restores_service(db_session, subscriber, catalog_offer):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.void(db_session, str(invoice.id))

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(sub.id), reason=EnforcementReason.overdue
    )


def test_bulk_void_overdue_invoice_restores_service(
    db_session, subscriber, catalog_offer
):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.bulk_void(
        db_session, InvoiceBulkVoidRequest(invoice_ids=[str(invoice.id)])
    )

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
