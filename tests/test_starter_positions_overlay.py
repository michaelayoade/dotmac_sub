"""Starter readers: fact eligibility and transient overlay mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.models.billing import Invoice, InvoiceLine, StarterInvoiceLink
from app.services.billing import starter_positions as readers


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


def _money(amount: str):  # noqa: ANN202
    return SimpleNamespace(amount=Decimal(amount))


def _fact():  # noqa: ANN202
    return SimpleNamespace(
        document_number="SET-2026-0007",
        subtotal=_money("100.00"),
        tax_total=_money("7.50"),
        grand_total=_money("107.50"),
        issued_at=datetime(2026, 10, 19, tzinfo=UTC),
        due_at=datetime(2026, 11, 18, tzinfo=UTC),
        lines=(
            SimpleNamespace(
                description="Fiber access (monthly)",
                quantity=Decimal("1"),
                unit_amount=_money("100.00"),
                pre_tax_amount=_money("100.00"),
            ),
        ),
    )


def test_unlinked_invoice_has_no_starter_fact() -> None:
    assert readers.starter_invoice_fact(_Db(None), _invoice()) is None


def test_linked_invoice_without_fact_version_has_no_starter_fact() -> None:
    invoice = _invoice()
    db = _Db(_link(invoice, fact_version=None))
    assert readers.starter_invoice_fact(db, invoice) is None


def test_overlay_carries_fact_fields_and_leaves_mirror_untouched() -> None:
    invoice = _invoice()
    overlaid = readers.apply_fact_overlay(invoice, _fact())  # type: ignore[arg-type]

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
