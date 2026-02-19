"""Tests for billing service submodules.

Covers: invoices, invoice lines, payments, payment allocations,
tax rates, ledger entries, billing runs, payment providers,
collection accounts, payment channels, configuration helpers,
and billing reporting.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    CollectionAccountType,
    Invoice,
    InvoiceStatus,
    LedgerEntryType,
    LedgerSource,
    PaymentChannelType,
    PaymentProviderType,
    PaymentStatus,
    TaxApplication,
)
from app.models.subscriber import Subscriber
from app.schemas.billing import (
    CollectionAccountCreate,
    CollectionAccountUpdate,
    InvoiceBulkVoidRequest,
    InvoiceBulkWriteOffRequest,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
    LedgerEntryCreate,
    LedgerEntryUpdate,
    PaymentAllocationApply,
    PaymentChannelCreate,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentMethodCreate,
    PaymentMethodUpdate,
    PaymentProviderCreate,
    PaymentProviderUpdate,
    PaymentUpdate,
    TaxRateCreate,
    TaxRateUpdate,
)
from app.services import billing as billing_service
from app.services.billing._common import (
    _validate_invoice_line_amount,
    _validate_invoice_totals,
)
from app.services.billing.configuration import _parse_bool, _parse_json
from app.services.billing.reporting import BillingReporting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subscriber(db_session: object) -> Subscriber:
    """Create a minimal subscriber for billing tests."""
    sub = Subscriber(
        first_name="Billing",
        last_name="Test",
        email=f"billing-{uuid.uuid4().hex[:8]}@test.local",
    )
    db = db_session
    assert isinstance(db, Session)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _make_invoice(
    db_session: Session,
    account_id: uuid.UUID,
    *,
    currency: str = "USD",
    subtotal: Decimal = Decimal("0.00"),
    tax_total: Decimal = Decimal("0.00"),
    total: Decimal = Decimal("0.00"),
    balance_due: Decimal = Decimal("0.00"),
    status: InvoiceStatus = InvoiceStatus.draft,
) -> Invoice:
    """Shortcut to create an invoice via the service."""
    return cast(
        Invoice,
        billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            currency=currency,
            subtotal=subtotal,
            tax_total=tax_total,
            total=total,
            balance_due=balance_due,
            status=status,
        ),
        ),
    )


# ============================================================================
# Invoice CRUD
# ============================================================================


class TestInvoiceCRUD:
    def test_create_invoice(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        assert invoice.id is not None
        assert invoice.account_id == subscriber.id
        assert invoice.currency == "USD"
        assert invoice.status == InvoiceStatus.draft

    def test_get_invoice(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        fetched = billing_service.invoices.get(db_session, str(invoice.id))
        assert fetched.id == invoice.id

    def test_get_invoice_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_invoices(self, db_session, subscriber):
        _make_invoice(db_session, subscriber.id)
        _make_invoice(db_session, subscriber.id)
        results = billing_service.invoices.list(
            db_session,
            account_id=str(subscriber.id),
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 2

    def test_list_invoices_filter_by_status(self, db_session, subscriber):
        _make_invoice(db_session, subscriber.id, status=InvoiceStatus.draft)
        results = billing_service.invoices.list(
            db_session,
            account_id=str(subscriber.id),
            status="draft",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        for inv in results:
            assert inv.status == InvoiceStatus.draft

    def test_update_invoice(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        updated = billing_service.invoices.update(
            db_session,
            str(invoice.id),
            InvoiceUpdate(memo="Updated memo"),
        )
        assert updated.memo == "Updated memo"

    def test_update_invoice_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.update(
                db_session,
                str(uuid.uuid4()),
                InvoiceUpdate(memo="nope"),
            )
        assert exc.value.status_code == 404

    def test_update_invoice_currency_mismatch(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id, currency="USD")
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.update(
                db_session,
                str(invoice.id),
                InvoiceUpdate(currency="EUR"),
            )
        assert exc.value.status_code == 400
        assert "Currency" in exc.value.detail

    def test_delete_invoice_soft(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoices.delete(db_session, str(invoice.id))
        db_session.refresh(invoice)
        assert invoice.is_active is False

    def test_delete_invoice_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404


# ============================================================================
# Invoice write-off and void
# ============================================================================


class TestInvoiceWriteOffVoid:
    def test_write_off_invoice(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        result = billing_service.invoices.write_off(db_session, str(invoice.id))
        assert result.balance_due == Decimal("0.00")
        assert result.status == InvoiceStatus.void

    def test_write_off_invoice_zero_balance(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        )
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.write_off(db_session, str(invoice.id))
        assert exc.value.status_code == 400

    def test_void_invoice(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("50.00"),
            total=Decimal("50.00"),
            balance_due=Decimal("50.00"),
        )
        result = billing_service.invoices.void(
            db_session, str(invoice.id), memo="Void reason"
        )
        assert result.status == InvoiceStatus.void
        assert result.balance_due == Decimal("0.00")
        assert result.memo == "Void reason"

    def test_bulk_write_off(self, db_session, subscriber):
        inv1 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            balance_due=Decimal("10.00"),
        )
        inv2 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("20.00"),
            total=Decimal("20.00"),
            balance_due=Decimal("20.00"),
        )
        payload = InvoiceBulkWriteOffRequest(
            invoice_ids=[inv1.id, inv2.id], memo="Bulk write-off"
        )
        result = billing_service.invoices.bulk_write_off_response(db_session, payload)
        assert result["updated"] == 2

    def test_bulk_write_off_empty_ids(self, db_session):
        payload = InvoiceBulkWriteOffRequest(invoice_ids=[])
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.bulk_write_off(db_session, payload)
        assert exc.value.status_code == 400

    def test_bulk_void(self, db_session, subscriber):
        inv1 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            balance_due=Decimal("10.00"),
        )
        inv2 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("20.00"),
            total=Decimal("20.00"),
            balance_due=Decimal("20.00"),
        )
        payload = InvoiceBulkVoidRequest(
            invoice_ids=[inv1.id, inv2.id], memo="Bulk void"
        )
        result = billing_service.invoices.bulk_void_response(db_session, payload)
        assert result["updated"] == 2

    def test_bulk_void_empty_ids(self, db_session):
        payload = InvoiceBulkVoidRequest(invoice_ids=[])
        with pytest.raises(HTTPException) as exc:
            billing_service.invoices.bulk_void(db_session, payload)
        assert exc.value.status_code == 400


# ============================================================================
# Invoice Line CRUD and recalculation
# ============================================================================


class TestInvoiceLineCRUD:
    def test_create_invoice_line(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        line = billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Service fee",
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
            ),
        )
        assert line.amount == Decimal("20.00")
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("20.00")
        assert invoice.total == Decimal("20.00")
        assert invoice.balance_due == Decimal("20.00")

    def test_create_invoice_line_not_found_invoice(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoice_lines.create(
                db_session,
                InvoiceLineCreate(
                    invoice_id=uuid.uuid4(),
                    description="Nope",
                    quantity=Decimal("1"),
                    unit_price=Decimal("5.00"),
                ),
            )
        assert exc.value.status_code == 404

    def test_get_invoice_line(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        line = billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Item",
                quantity=Decimal("1"),
                unit_price=Decimal("5.00"),
            ),
        )
        fetched = billing_service.invoice_lines.get(db_session, str(line.id))
        assert fetched.id == line.id

    def test_get_invoice_line_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoice_lines.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_invoice_lines(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Item A",
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
            ),
        )
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Item B",
                quantity=Decimal("1"),
                unit_price=Decimal("20.00"),
            ),
        )
        results = billing_service.invoice_lines.list(
            db_session,
            invoice_id=str(invoice.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 2

    def test_update_invoice_line_recalculates(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        line = billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Service",
                quantity=Decimal("1"),
                unit_price=Decimal("15.00"),
            ),
        )
        updated = billing_service.invoice_lines.update(
            db_session,
            str(line.id),
            InvoiceLineUpdate(quantity=Decimal("3")),
        )
        assert updated.amount == Decimal("45.00")
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("45.00")
        assert invoice.total == Decimal("45.00")

    def test_update_invoice_line_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoice_lines.update(
                db_session,
                str(uuid.uuid4()),
                InvoiceLineUpdate(description="Nope"),
            )
        assert exc.value.status_code == 404

    def test_delete_invoice_line_recalculates(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        line = billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="To delete",
                quantity=Decimal("1"),
                unit_price=Decimal("25.00"),
            ),
        )
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("25.00")

        billing_service.invoice_lines.delete(db_session, str(line.id))
        db_session.refresh(invoice)
        # Line is soft-deleted so it's excluded from recalculation
        assert invoice.subtotal == Decimal("0.00")
        assert invoice.balance_due == Decimal("0.00")

    def test_delete_invoice_line_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.invoice_lines.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_multiple_lines_recalculate_correctly(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Line A",
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
            ),
        )
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Line B",
                quantity=Decimal("3"),
                unit_price=Decimal("5.00"),
            ),
        )
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("35.00")
        assert invoice.total == Decimal("35.00")
        assert invoice.balance_due == Decimal("35.00")


# ============================================================================
# Tax Rate CRUD
# ============================================================================


class TestTaxRateCRUD:
    def test_create_tax_rate(self, db_session):
        rate = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="VAT", code="VAT15", rate=Decimal("15.0000")),
        )
        assert rate.id is not None
        assert rate.name == "VAT"
        assert rate.rate == Decimal("15.0000")

    def test_get_tax_rate(self, db_session):
        rate = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="Sales Tax", rate=Decimal("7.5000")),
        )
        fetched = billing_service.tax_rates.get(db_session, str(rate.id))
        assert fetched.name == "Sales Tax"

    def test_get_tax_rate_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.tax_rates.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_tax_rates(self, db_session):
        billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="Rate A", rate=Decimal("5.0000")),
        )
        results = billing_service.tax_rates.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_tax_rate(self, db_session):
        rate = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="Old Name", rate=Decimal("10.0000")),
        )
        updated = billing_service.tax_rates.update(
            db_session,
            str(rate.id),
            TaxRateUpdate(name="New Name", rate=Decimal("12.0000")),
        )
        assert updated.name == "New Name"
        assert updated.rate == Decimal("12.0000")

    def test_update_tax_rate_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.tax_rates.update(
                db_session,
                str(uuid.uuid4()),
                TaxRateUpdate(name="Nope"),
            )
        assert exc.value.status_code == 404

    def test_delete_tax_rate_soft(self, db_session):
        rate = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="To Delete", rate=Decimal("1.0000")),
        )
        billing_service.tax_rates.delete(db_session, str(rate.id))
        db_session.refresh(rate)
        assert rate.is_active is False

    def test_delete_tax_rate_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.tax_rates.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404


# ============================================================================
# Invoice Line with Tax Rate
# ============================================================================


class TestInvoiceLineWithTax:
    def test_line_with_exclusive_tax(self, db_session, subscriber):
        tax = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="VAT", rate=Decimal("10.0000")),
        )
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Taxed item",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                tax_rate_id=tax.id,
                tax_application=TaxApplication.exclusive,
            ),
        )
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("100.00")
        assert invoice.tax_total == Decimal("10.00")
        assert invoice.total == Decimal("110.00")

    def test_line_with_inclusive_tax(self, db_session, subscriber):
        tax = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="GST", rate=Decimal("10.0000")),
        )
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Inclusive tax item",
                quantity=Decimal("1"),
                unit_price=Decimal("110.00"),
                tax_rate_id=tax.id,
                tax_application=TaxApplication.inclusive,
            ),
        )
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("110.00")
        assert invoice.tax_total == Decimal("10.00")
        assert invoice.total == Decimal("120.00")

    def test_line_with_exempt_tax(self, db_session, subscriber):
        tax = billing_service.tax_rates.create(
            db_session,
            TaxRateCreate(name="Exempt Rate", rate=Decimal("15.0000")),
        )
        invoice = _make_invoice(db_session, subscriber.id)
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Exempt item",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                tax_rate_id=tax.id,
                tax_application=TaxApplication.exempt,
            ),
        )
        db_session.refresh(invoice)
        assert invoice.subtotal == Decimal("100.00")
        assert invoice.tax_total == Decimal("0.00")
        assert invoice.total == Decimal("100.00")


# ============================================================================
# Ledger Entry CRUD
# ============================================================================


class TestLedgerEntryCRUD:
    def test_create_ledger_entry(self, db_session, subscriber):
        entry = billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.invoice,
                amount=Decimal("50.00"),
                currency="USD",
                memo="Test debit",
            ),
        )
        assert entry.id is not None
        assert entry.entry_type == LedgerEntryType.debit
        assert entry.amount == Decimal("50.00")

    def test_get_ledger_entry(self, db_session, subscriber):
        entry = billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("25.00"),
            ),
        )
        fetched = billing_service.ledger_entries.get(db_session, str(entry.id))
        assert fetched.id == entry.id

    def test_get_ledger_entry_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.ledger_entries.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_ledger_entries(self, db_session, subscriber):
        billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.invoice,
                amount=Decimal("10.00"),
            ),
        )
        results = billing_service.ledger_entries.list(
            db_session,
            account_id=str(subscriber.id),
            entry_type=None,
            source=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_list_ledger_entries_filter_by_type(self, db_session, subscriber):
        billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("5.00"),
            ),
        )
        results = billing_service.ledger_entries.list(
            db_session,
            account_id=str(subscriber.id),
            entry_type="credit",
            source=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        for e in results:
            assert e.entry_type == LedgerEntryType.credit

    def test_update_ledger_entry(self, db_session, subscriber):
        entry = billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("30.00"),
            ),
        )
        updated = billing_service.ledger_entries.update(
            db_session,
            str(entry.id),
            LedgerEntryUpdate(memo="Updated memo"),
        )
        assert updated.memo == "Updated memo"

    def test_update_ledger_entry_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.ledger_entries.update(
                db_session,
                str(uuid.uuid4()),
                LedgerEntryUpdate(memo="nope"),
            )
        assert exc.value.status_code == 404

    def test_delete_ledger_entry_soft(self, db_session, subscriber):
        entry = billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.other,
                amount=Decimal("5.00"),
            ),
        )
        billing_service.ledger_entries.delete(db_session, str(entry.id))
        db_session.refresh(entry)
        assert entry.is_active is False

    def test_delete_ledger_entry_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.ledger_entries.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_create_ledger_entry_with_invoice(self, db_session, subscriber):
        invoice = _make_invoice(db_session, subscriber.id)
        entry = billing_service.ledger_entries.create(
            db_session,
            LedgerEntryCreate(
                account_id=subscriber.id,
                invoice_id=invoice.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.invoice,
                amount=Decimal("40.00"),
                currency="USD",
            ),
        )
        assert entry.invoice_id == invoice.id


# ============================================================================
# Payment Provider CRUD
# ============================================================================


class TestPaymentProviderCRUD:
    def test_create_payment_provider(self, db_session):
        provider = billing_service.payment_providers.create(
            db_session,
            PaymentProviderCreate(
                name=f"Stripe-{uuid.uuid4().hex[:6]}",
                provider_type=PaymentProviderType.stripe,
            ),
        )
        assert provider.id is not None
        assert provider.provider_type == PaymentProviderType.stripe

    def test_get_payment_provider(self, db_session):
        provider = billing_service.payment_providers.create(
            db_session,
            PaymentProviderCreate(
                name=f"PayPal-{uuid.uuid4().hex[:6]}",
                provider_type=PaymentProviderType.paypal,
            ),
        )
        fetched = billing_service.payment_providers.get(db_session, str(provider.id))
        assert fetched.id == provider.id

    def test_get_payment_provider_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_providers.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_payment_providers(self, db_session):
        billing_service.payment_providers.create(
            db_session,
            PaymentProviderCreate(
                name=f"Prov-{uuid.uuid4().hex[:6]}",
            ),
        )
        results = billing_service.payment_providers.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_payment_provider(self, db_session):
        provider = billing_service.payment_providers.create(
            db_session,
            PaymentProviderCreate(
                name=f"UpdProv-{uuid.uuid4().hex[:6]}",
            ),
        )
        updated = billing_service.payment_providers.update(
            db_session,
            str(provider.id),
            PaymentProviderUpdate(notes="Updated note"),
        )
        assert updated.notes == "Updated note"

    def test_update_payment_provider_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_providers.update(
                db_session,
                str(uuid.uuid4()),
                PaymentProviderUpdate(notes="nope"),
            )
        assert exc.value.status_code == 404

    def test_delete_payment_provider_soft(self, db_session):
        provider = billing_service.payment_providers.create(
            db_session,
            PaymentProviderCreate(
                name=f"DelProv-{uuid.uuid4().hex[:6]}",
            ),
        )
        billing_service.payment_providers.delete(db_session, str(provider.id))
        db_session.refresh(provider)
        assert provider.is_active is False

    def test_delete_payment_provider_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_providers.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404


# ============================================================================
# Collection Account CRUD
# ============================================================================


class TestCollectionAccountCRUD:
    def test_create_collection_account(self, db_session):
        account = billing_service.collection_accounts.create(
            db_session,
            CollectionAccountCreate(
                name=f"Primary-{uuid.uuid4().hex[:6]}",
                account_type=CollectionAccountType.bank,
                currency="USD",
            ),
        )
        assert account.id is not None
        assert account.currency == "USD"

    def test_get_collection_account(self, db_session):
        account = billing_service.collection_accounts.create(
            db_session,
            CollectionAccountCreate(
                name=f"Get-{uuid.uuid4().hex[:6]}",
                currency="USD",
            ),
        )
        fetched = billing_service.collection_accounts.get(db_session, str(account.id))
        assert fetched.id == account.id

    def test_get_collection_account_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.collection_accounts.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_collection_accounts(self, db_session):
        billing_service.collection_accounts.create(
            db_session,
            CollectionAccountCreate(
                name=f"List-{uuid.uuid4().hex[:6]}",
                currency="USD",
            ),
        )
        results = billing_service.collection_accounts.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_collection_account(self, db_session):
        account = billing_service.collection_accounts.create(
            db_session,
            CollectionAccountCreate(
                name=f"Upd-{uuid.uuid4().hex[:6]}",
                currency="USD",
            ),
        )
        updated = billing_service.collection_accounts.update(
            db_session,
            str(account.id),
            CollectionAccountUpdate(notes="Updated"),
        )
        assert updated.notes == "Updated"

    def test_delete_collection_account_soft(self, db_session):
        account = billing_service.collection_accounts.create(
            db_session,
            CollectionAccountCreate(
                name=f"Del-{uuid.uuid4().hex[:6]}",
                currency="USD",
            ),
        )
        billing_service.collection_accounts.delete(db_session, str(account.id))
        db_session.refresh(account)
        assert account.is_active is False


# ============================================================================
# Payment Channel CRUD
# ============================================================================


class TestPaymentChannelCRUD:
    def test_create_payment_channel(self, db_session):
        channel = billing_service.payment_channels.create(
            db_session,
            PaymentChannelCreate(
                name=f"Card-{uuid.uuid4().hex[:6]}",
                channel_type=PaymentChannelType.card,
            ),
        )
        assert channel.id is not None
        assert channel.channel_type == PaymentChannelType.card

    def test_get_payment_channel(self, db_session):
        channel = billing_service.payment_channels.create(
            db_session,
            PaymentChannelCreate(
                name=f"GetCh-{uuid.uuid4().hex[:6]}",
            ),
        )
        fetched = billing_service.payment_channels.get(db_session, str(channel.id))
        assert fetched.id == channel.id

    def test_get_payment_channel_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_channels.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_payment_channels(self, db_session):
        billing_service.payment_channels.create(
            db_session,
            PaymentChannelCreate(
                name=f"ListCh-{uuid.uuid4().hex[:6]}",
            ),
        )
        results = billing_service.payment_channels.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_payment_channel(self, db_session):
        channel = billing_service.payment_channels.create(
            db_session,
            PaymentChannelCreate(
                name=f"UpdCh-{uuid.uuid4().hex[:6]}",
            ),
        )
        updated = billing_service.payment_channels.update(
            db_session,
            str(channel.id),
            PaymentChannelUpdate(notes="Updated channel"),
        )
        assert updated.notes == "Updated channel"

    def test_delete_payment_channel_soft(self, db_session):
        channel = billing_service.payment_channels.create(
            db_session,
            PaymentChannelCreate(
                name=f"DelCh-{uuid.uuid4().hex[:6]}",
            ),
        )
        billing_service.payment_channels.delete(db_session, str(channel.id))
        db_session.refresh(channel)
        assert channel.is_active is False


# ============================================================================
# Payment Method CRUD
# ============================================================================


class TestPaymentMethodCRUD:
    def test_create_payment_method(self, db_session, subscriber):
        method = billing_service.payment_methods.create(
            db_session,
            PaymentMethodCreate(
                account_id=subscriber.id,
                label="My Card",
                last4="1234",
            ),
        )
        assert method.id is not None
        assert method.last4 == "1234"

    def test_get_payment_method(self, db_session, subscriber):
        method = billing_service.payment_methods.create(
            db_session,
            PaymentMethodCreate(
                account_id=subscriber.id,
                label="Get Card",
            ),
        )
        fetched = billing_service.payment_methods.get(db_session, str(method.id))
        assert fetched.id == method.id

    def test_get_payment_method_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_methods.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_payment_methods(self, db_session, subscriber):
        billing_service.payment_methods.create(
            db_session,
            PaymentMethodCreate(
                account_id=subscriber.id,
                label="List Card",
            ),
        )
        results = billing_service.payment_methods.list(
            db_session,
            account_id=str(subscriber.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_payment_method(self, db_session, subscriber):
        method = billing_service.payment_methods.create(
            db_session,
            PaymentMethodCreate(
                account_id=subscriber.id,
                label="Old Label",
            ),
        )
        updated = billing_service.payment_methods.update(
            db_session,
            str(method.id),
            PaymentMethodUpdate(label="New Label"),
        )
        assert updated.label == "New Label"

    def test_delete_payment_method_soft(self, db_session, subscriber):
        method = billing_service.payment_methods.create(
            db_session,
            PaymentMethodCreate(
                account_id=subscriber.id,
                label="Del Card",
            ),
        )
        billing_service.payment_methods.delete(db_session, str(method.id))
        db_session.refresh(method)
        assert method.is_active is False


# ============================================================================
# Payment CRUD
# ============================================================================


class TestPaymentCRUD:
    def test_create_payment(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=PaymentStatus.pending,
            ),
        )
        assert payment.id is not None
        assert payment.amount == Decimal("100.00")
        assert payment.currency == "USD"

    def test_create_payment_zero_amount(self, db_session, subscriber):
        with pytest.raises(Exception):
            billing_service.payments.create(
                db_session,
                PaymentCreate(
                    account_id=subscriber.id,
                    amount=Decimal("0"),
                    currency="USD",
                ),
            )

    def test_get_payment(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                currency="USD",
            ),
        )
        fetched = billing_service.payments.get(db_session, str(payment.id))
        assert fetched.id == payment.id

    def test_get_payment_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payments.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_payments(self, db_session, subscriber):
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("30.00"),
                currency="USD",
            ),
        )
        results = billing_service.payments.list(
            db_session,
            account_id=str(subscriber.id),
            invoice_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        assert len(results) >= 1

    def test_update_payment(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("70.00"),
                currency="USD",
            ),
        )
        updated = billing_service.payments.update(
            db_session,
            str(payment.id),
            PaymentUpdate(memo="Updated payment memo"),
        )
        assert updated.memo == "Updated payment memo"

    def test_update_payment_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payments.update(
                db_session,
                str(uuid.uuid4()),
                PaymentUpdate(memo="nope"),
            )
        assert exc.value.status_code == 404

    def test_delete_payment_soft(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("15.00"),
                currency="USD",
            ),
        )
        billing_service.payments.delete(db_session, str(payment.id))
        db_session.refresh(payment)
        assert payment.is_active is False

    def test_delete_payment_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payments.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404


# ============================================================================
# Payment with explicit allocations
# ============================================================================


class TestPaymentWithAllocations:
    def test_explicit_allocation_to_invoice(self, db_session, subscriber):
        """Create a payment with explicit allocation to an invoice."""
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            status=InvoiceStatus.issued,
        )
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id,
                        amount=Decimal("100.00"),
                    ),
                ],
            ),
        )
        allocations = billing_service.payment_allocations.list(
            db_session,
            payment_id=str(payment.id),
            invoice_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(allocations) >= 1
        total_allocated = sum(a.amount for a in allocations)
        assert total_allocated == Decimal("100.00")
        # Invoice should be paid
        db_session.refresh(invoice)
        assert invoice.balance_due == Decimal("0.00")

    def test_partial_allocation(self, db_session, subscriber):
        """Allocate less than the invoice balance."""
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            status=InvoiceStatus.issued,
        )
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("60.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id,
                        amount=Decimal("60.00"),
                    ),
                ],
            ),
        )
        db_session.refresh(invoice)
        assert invoice.balance_due == Decimal("40.00")
        assert invoice.status == InvoiceStatus.partially_paid

    def test_multiple_allocations(self, db_session, subscriber):
        """Allocate one payment across multiple invoices."""
        inv1 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("50.00"),
            total=Decimal("50.00"),
            balance_due=Decimal("50.00"),
            status=InvoiceStatus.issued,
        )
        inv2 = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("30.00"),
            total=Decimal("30.00"),
            balance_due=Decimal("30.00"),
            status=InvoiceStatus.issued,
        )
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("80.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=inv1.id,
                        amount=Decimal("50.00"),
                    ),
                    PaymentAllocationApply(
                        invoice_id=inv2.id,
                        amount=Decimal("30.00"),
                    ),
                ],
            ),
        )
        allocations = billing_service.payment_allocations.list(
            db_session,
            payment_id=str(payment.id),
            invoice_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(allocations) >= 2
        total_allocated = sum(a.amount for a in allocations)
        assert total_allocated == Decimal("80.00")


# ============================================================================
# Payment allocation listing and deletion
# ============================================================================


class TestPaymentAllocationCRUD:
    def test_list_allocations(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            status=InvoiceStatus.issued,
        )
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id,
                        amount=Decimal("100.00"),
                    ),
                ],
            ),
        )
        allocations = billing_service.payment_allocations.list(
            db_session,
            payment_id=str(payment.id),
            invoice_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(allocations) >= 1

    def test_list_allocations_by_invoice(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            status=InvoiceStatus.issued,
        )
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id,
                        amount=Decimal("100.00"),
                    ),
                ],
            ),
        )
        allocations = billing_service.payment_allocations.list(
            db_session,
            payment_id=None,
            invoice_id=str(invoice.id),
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(allocations) >= 1

    def test_delete_allocation(self, db_session, subscriber):
        invoice = _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            status=InvoiceStatus.issued,
        )
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id,
                        amount=Decimal("100.00"),
                    ),
                ],
            ),
        )
        allocations = billing_service.payment_allocations.list(
            db_session,
            payment_id=str(payment.id),
            invoice_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        assert len(allocations) >= 1
        alloc = allocations[0]
        billing_service.payment_allocations.delete(db_session, str(alloc.id))
        # After deleting allocation, invoice should have balance restored
        db_session.refresh(invoice)
        assert invoice.balance_due > Decimal("0.00")

    def test_delete_allocation_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payment_allocations.delete(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404


# ============================================================================
# Billing Run
# ============================================================================


class TestBillingRun:
    def test_get_billing_run_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.billing_runs.get(db_session, str(uuid.uuid4()))
        assert exc.value.status_code == 404

    def test_list_billing_runs_empty(self, db_session):
        results = billing_service.billing_runs.list(
            db_session,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        # May or may not have results; just ensure no error
        assert isinstance(results, list)

    def test_get_billing_run(self, db_session):
        run = BillingRun(
            run_at=datetime.now(UTC),
            status=BillingRunStatus.success,
            started_at=datetime.now(UTC),
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        fetched = billing_service.billing_runs.get(db_session, str(run.id))
        assert fetched.id == run.id
        assert fetched.status == BillingRunStatus.success

    def test_list_billing_runs_filter_by_status(self, db_session):
        run = BillingRun(
            run_at=datetime.now(UTC),
            status=BillingRunStatus.success,
            started_at=datetime.now(UTC),
        )
        db_session.add(run)
        db_session.commit()

        results = billing_service.billing_runs.list(
            db_session,
            status="success",
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        for r in results:
            assert r.status == BillingRunStatus.success


# ============================================================================
# Pure functions / helpers
# ============================================================================


class TestPureFunctions:
    """Tests for pure/helper functions in the billing modules."""

    def test_validate_invoice_line_amount_auto_calc(self):
        result = _validate_invoice_line_amount(
            Decimal("3"), Decimal("10.00"), None
        )
        assert result == Decimal("30.00")

    def test_validate_invoice_line_amount_correct(self):
        result = _validate_invoice_line_amount(
            Decimal("2"), Decimal("5.00"), Decimal("10.00")
        )
        assert result == Decimal("10.00")

    def test_validate_invoice_line_amount_mismatch(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_line_amount(
                Decimal("2"), Decimal("5.00"), Decimal("99.00")
            )
        assert exc.value.status_code == 400

    def test_validate_invoice_line_amount_negative(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_line_amount(
                Decimal("1"), Decimal("10.00"), Decimal("-5.00")
            )
        assert exc.value.status_code == 400

    def test_validate_invoice_line_zero_quantity(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_line_amount(
                Decimal("0"), Decimal("10.00"), None
            )
        assert exc.value.status_code == 400

    def test_validate_invoice_line_negative_unit_price(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_line_amount(
                Decimal("1"), Decimal("-5.00"), None
            )
        assert exc.value.status_code == 400

    def test_validate_invoice_totals_valid(self):
        # Should not raise
        _validate_invoice_totals({
            "subtotal": Decimal("100.00"),
            "tax_total": Decimal("10.00"),
            "total": Decimal("110.00"),
            "balance_due": Decimal("110.00"),
        })

    def test_validate_invoice_totals_negative_subtotal(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_totals({"subtotal": Decimal("-1.00")})
        assert exc.value.status_code == 400

    def test_validate_invoice_totals_balance_exceeds_total(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_totals({
                "total": Decimal("50.00"),
                "balance_due": Decimal("100.00"),
            })
        assert exc.value.status_code == 400

    def test_validate_invoice_totals_total_below_subtotal_plus_tax(self):
        with pytest.raises(HTTPException) as exc:
            _validate_invoice_totals({
                "subtotal": Decimal("100.00"),
                "tax_total": Decimal("10.00"),
                "total": Decimal("90.00"),
            })
        assert exc.value.status_code == 400


# ============================================================================
# Configuration helpers
# ============================================================================


class TestConfigurationHelpers:
    def test_parse_bool_on(self):
        assert _parse_bool("on") is True

    def test_parse_bool_true(self):
        assert _parse_bool("true") is True

    def test_parse_bool_one(self):
        assert _parse_bool("1") is True

    def test_parse_bool_yes(self):
        assert _parse_bool("yes") is True

    def test_parse_bool_false(self):
        assert _parse_bool("off") is False
        assert _parse_bool("false") is False
        assert _parse_bool(None) is False

    def test_parse_json_valid(self):
        result = _parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_none(self):
        assert _parse_json(None) is None

    def test_parse_json_empty(self):
        assert _parse_json("") is None

    def test_parse_json_invalid(self):
        with pytest.raises(HTTPException) as exc:
            _parse_json("not valid json")
        assert exc.value.status_code == 400


# ============================================================================
# Reporting helpers
# ============================================================================


class TestReportingHelpers:
    def test_overview_stats_empty(self, db_session):
        """Test billing reporting overview with no invoices."""
        stats = BillingReporting.get_overview_stats(db_session)
        assert "total_revenue" in stats
        assert "pending_amount" in stats
        assert "overdue_amount" in stats
        assert "total_invoices" in stats
        assert isinstance(stats["total_revenue"], float)

    def test_overview_stats_with_invoices(self, db_session, subscriber):
        """Test overview stats correctly sums paid invoices."""
        _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("0.00"),
            status=InvoiceStatus.paid,
        )
        _make_invoice(
            db_session,
            subscriber.id,
            subtotal=Decimal("50.00"),
            total=Decimal("50.00"),
            balance_due=Decimal("50.00"),
            status=InvoiceStatus.issued,
        )
        stats = BillingReporting.get_overview_stats(db_session)
        assert stats["paid_count"] >= 1
        assert stats["total_revenue"] >= 100.0

    def test_ar_aging_buckets(self, db_session, subscriber):
        """Test AR aging buckets returns correct structure."""
        result = BillingReporting.get_ar_aging_buckets(db_session)
        assert "buckets" in result
        assert "totals" in result
        assert "current" in result["buckets"]
        assert "1_30" in result["buckets"]
        assert "31_60" in result["buckets"]
        assert "61_90" in result["buckets"]
        assert "90_plus" in result["buckets"]


# ============================================================================
# Payment mark_status
# ============================================================================


class TestPaymentMarkStatus:
    def test_mark_status_succeeded(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                currency="USD",
                status=PaymentStatus.pending,
            ),
        )
        billing_service.payments.mark_status(
            db_session, str(payment.id), PaymentStatus.succeeded
        )
        db_session.refresh(payment)
        assert payment.status == PaymentStatus.succeeded
        assert payment.paid_at is not None

    def test_mark_status_failed(self, db_session, subscriber):
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                currency="USD",
                status=PaymentStatus.pending,
            ),
        )
        billing_service.payments.mark_status(
            db_session, str(payment.id), PaymentStatus.failed
        )
        db_session.refresh(payment)
        assert payment.status == PaymentStatus.failed

    def test_mark_status_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc:
            billing_service.payments.mark_status(
                db_session, str(uuid.uuid4()), PaymentStatus.succeeded
            )
        assert exc.value.status_code == 404
