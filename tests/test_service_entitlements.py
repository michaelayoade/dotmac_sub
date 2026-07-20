from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    ServiceEntitlement,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    ensure_prepaid_entitlements_for_paid_invoice,
)


def test_paid_prepaid_invoice_entitlement_uses_base_line_only(
    db_session, subscriber_account, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    period_start = datetime(2026, 8, 1, tzinfo=UTC)
    period_end = datetime(2026, 9, 1, tzinfo=UTC)
    invoice = Invoice(
        account_id=subscriber_account.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("1200.00"),
        total=Decimal("1200.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=period_start,
        billing_period_end=period_end,
        issued_at=period_start,
        paid_at=period_start,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add_all(
        [
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Base service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.00"),
                amount=Decimal("1000.00"),
                metadata_={
                    "kind": "base_subscription",
                    "billing_period_start": period_start.isoformat(),
                    "billing_period_end": period_end.isoformat(),
                },
            ),
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Recurring add-on",
                quantity=Decimal("1.000"),
                unit_price=Decimal("200.00"),
                amount=Decimal("200.00"),
                metadata_={
                    "kind": "recurring_addon",
                    "billing_period_start": period_start.isoformat(),
                    "billing_period_end": period_end.isoformat(),
                },
            ),
        ]
    )
    db_session.flush()

    created = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)

    assert len(created) == 1
    entitlement = db_session.query(ServiceEntitlement).one()
    assert entitlement.amount_funded == Decimal("1000.00")
    assert entitlement.starts_at == period_start
    assert entitlement.ends_at == period_end


def test_paid_invoice_with_balance_does_not_create_entitlement(
    db_session, subscriber_account, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    invoice = Invoice(
        account_id=subscriber_account.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("1000.00"),
        total=Decimal("1000.00"),
        balance_due=Decimal("50.00"),
        billing_period_start=datetime.now(UTC),
        billing_period_end=datetime.now(UTC) + timedelta(days=30),
        issued_at=datetime.now(UTC),
        paid_at=datetime.now(UTC),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Base service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.00"),
            amount=Decimal("1000.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.flush()

    created = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)

    assert created == []
    assert db_session.query(ServiceEntitlement).count() == 0


def test_wallet_debit_entitlement_is_idempotent_by_ledger_entry(
    db_session, subscriber_account, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    ledger_entry = LedgerEntry(
        account_id=subscriber_account.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=Decimal("1000.00"),
        currency="NGN",
        memo="Prepaid service renewal",
    )
    db_session.add(ledger_entry)
    db_session.flush()
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    ends_at = datetime(2026, 9, 1, tzinfo=UTC)

    first = ensure_prepaid_entitlement_for_wallet_debit(
        db_session,
        subscription=subscription,
        ledger_entry=ledger_entry,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    second = ensure_prepaid_entitlement_for_wallet_debit(
        db_session,
        subscription=subscription,
        ledger_entry=ledger_entry,
        starts_at=starts_at,
        ends_at=ends_at,
    )

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert first.source_ledger_entry_id == ledger_entry.id
    assert db_session.query(ServiceEntitlement).count() == 1
