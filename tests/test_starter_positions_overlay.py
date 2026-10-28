"""Starter readers: fact eligibility and transient overlay mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import (
    AppliedTaxSnapshotV1,
    DueDateBasisV1,
    InvoiceDocumentFactV1,
    InvoiceLineFactV1,
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money

from app.models.billing import Invoice, InvoiceLine, StarterInvoiceLink
from app.services.billing import starter_positions as readers
from app.services.billing.invoices import invoice_tax_presentation
from app.services.operator_tenant import OPERATOR_TENANT_ID


class _Db:
    def __init__(self, link: StarterInvoiceLink | None) -> None:
        self._link = link

    def execute(self, _statement):  # noqa: ANN001, ANN202
        link = self._link

        class _Result:
            def scalar_one_or_none(self):
                return link

        return _Result()


def _invoice() -> Invoice:
    invoice = Invoice(currency="NGN")
    invoice.id = uuid4()
    invoice.invoice_number = "LEGACY-1"
    invoice.subtotal = Decimal("1.00")
    invoice.tax_total = Decimal("0.00")
    invoice.total = Decimal("1.00")
    invoice.balance_due = Decimal("1.00")
    invoice.lines = [
        InvoiceLine(
            description="Stale mirror line",
            quantity=Decimal("1"),
            unit_price=Decimal("1.00"),
            amount=Decimal("1.00"),
            is_active=True,
        )
    ]
    return invoice


def _link(invoice: Invoice, *, fact_version: int | None) -> StarterInvoiceLink:
    return StarterInvoiceLink(
        invoice_id=invoice.id,
        billing_account_id=uuid4(),
        document_id=uuid4(),
        source_fact_id="test",
        issued_fact_version=fact_version,
    )


def _money(amount: str | Decimal) -> Money:
    return Money.of(amount, Currency("NGN", 2))


def _fact(
    rates: tuple[Decimal, ...] = (Decimal("0.075"),),
) -> InvoiceDocumentFactV1:
    tax_snapshots = tuple(
        AppliedTaxSnapshotV1(
            treatment_code="vat_standard",
            jurisdiction_code="NGA",
            policy_id="overlay-fixture",
            policy_version="1",
            rate=rate,
            taxable_basis=_money("100.00"),
            tax_amount=_money(Decimal("100") * rate),
        )
        for rate in rates
    )
    tax_total = sum((tax.tax_amount.amount for tax in tax_snapshots), Decimal("0"))
    total = Decimal("100") + tax_total
    issued_at = datetime(2026, 10, 19, tzinfo=UTC)
    party = PartyDocumentSnapshotV1(
        legal_name="Overlay fixture",
        address=PostalAddressSnapshotV1("1 Fixture Road", "NGA"),
    )
    return InvoiceDocumentFactV1(
        scope=TenantScope(OPERATOR_TENANT_ID),
        invoice_id=uuid4(),
        fact_version=1,
        series_code="overlay-fixture",
        document_number="SET-2026-0007",
        document_kind="invoice",
        document_state="issued",
        document_profile_code="overlay-fixture",
        document_profile_version="1",
        currency="NGN",
        minor_units=2,
        subtotal=_money("100.00"),
        tax_total=_money(tax_total),
        grand_total=_money(total),
        issued_at=issued_at,
        due_at=datetime(2026, 11, 18, tzinfo=UTC),
        due_date_basis=DueDateBasisV1.unknown_unverified(
            source_authority="overlay-fixture", evidence_ref="overlay-fixture"
        ),
        seller_snapshot=party,
        customer_snapshot=party,
        payment_instructions=PaymentInstructionsSnapshotV1("bank_transfer"),
        brand_asset=PresentationAssetReferenceV1.none(),
        locale="en-NG",
        timezone="UTC",
        lines=(
            InvoiceLineFactV1(
                line_number=1,
                description="Fiber access (monthly)",
                quantity=Decimal("1"),
                unit_code="month",
                unit_amount=_money("100.00"),
                pre_tax_amount=_money("100.00"),
                discount_total=_money("0.00"),
                tax_amount=_money(tax_total),
                total_amount=_money(total),
                price_source_version="1",
            ),
        ),
        tax_snapshots=tax_snapshots,
        fx_snapshot=None,
        party_tax_identities=(),
        frozen_at=issued_at,
        source_authority="internal",
        correlation_id="overlay-fixture",
        presentation_model_digest="0" * 64,
    )


def test_unlinked_invoice_has_no_starter_fact() -> None:
    assert readers.starter_invoice_fact(_Db(None), _invoice()) is None


def test_linked_invoice_without_fact_version_has_no_starter_fact() -> None:
    invoice = _invoice()
    db = _Db(_link(invoice, fact_version=None))
    assert readers.starter_invoice_fact(db, invoice) is None


def test_overlay_carries_fact_fields_and_leaves_mirror_untouched() -> None:
    invoice = _invoice()
    overlaid = readers.apply_fact_overlay(invoice, _fact())

    assert overlaid.invoice_number == "SET-2026-0007"
    assert overlaid.subtotal == Decimal("100.00")
    assert overlaid.tax_total == Decimal("7.50")
    assert overlaid.total == Decimal("107.50")
    assert overlaid.lines[0].description == "Fiber access (monthly)"
    assert overlaid.lines[0].unit_price == Decimal("100.00")
    assert overlaid.lines[0].amount == Decimal("100.00")
    # Balance stays mirror-owned; the persistent row is untouched.
    assert overlaid.balance_due == Decimal("1.00")
    assert invoice.invoice_number == "LEGACY-1"
    assert invoice.total == Decimal("1.00")


@pytest.mark.parametrize(
    ("rates", "label"),
    [
        ((Decimal("0.075"), Decimal("0.025")), "VAT (2.5%, 7.5%)"),
        ((Decimal("0.07517"),), "VAT (7.517%)"),
        ((), "VAT"),
    ],
)
def test_overlay_tax_label_uses_exact_frozen_rates(
    rates: tuple[Decimal, ...], label: str
) -> None:
    invoice = _invoice()
    invoice.lines[0].tax_rate_percent_snapshot = Decimal("99.0000")

    overlaid = readers.apply_fact_overlay(invoice, _fact(rates))

    assert invoice_tax_presentation(overlaid).vat_label == label
    assert invoice.lines[0].tax_rate_percent_snapshot == Decimal("99.0000")


def test_legacy_tax_projection_uses_only_active_line_snapshots() -> None:
    invoice = _invoice()
    invoice.lines[0].tax_rate_percent_snapshot = Decimal("7.5000")
    invoice.lines.append(
        InvoiceLine(
            description="Retired rate",
            tax_rate_percent_snapshot=Decimal("99.0000"),
            is_active=False,
        )
    )

    assert invoice.tax_rate_percent_snapshots == (Decimal("7.5000"),)
    assert invoice_tax_presentation(invoice).vat_label == "VAT (7.5%)"
