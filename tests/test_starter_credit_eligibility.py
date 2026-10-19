"""Starter credit eligibility: only fully-applied Starter credits switch."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.models.billing import Invoice, StarterInvoiceLink, TaxRate
from app.services.billing import starter_credit_notes as credits


class _Db:
    def __init__(self, link: StarterInvoiceLink | None, tax_rate=None) -> None:
        self._link = link
        self._tax_rate = tax_rate

    def execute(self, _statement):  # noqa: ANN001, ANN202
        link = self._link

        class _Result:
            def scalar_one_or_none(self):
                return link

        return _Result()

    def get(self, _model, _id):  # noqa: ANN001, ANN202
        return self._tax_rate


def _invoice() -> Invoice:
    invoice = Invoice(currency="NGN")
    invoice.id = uuid4()
    return invoice


def _link(invoice: Invoice) -> StarterInvoiceLink:
    return StarterInvoiceLink(
        invoice_id=invoice.id,
        billing_account_id=uuid4(),
        document_id=uuid4(),
        source_fact_id="test",
    )


def _reason(db, **overrides):  # noqa: ANN001, ANN202
    kwargs = {
        "invoice": _invoice(),
        "credit_number": None,
        "tax_total": Decimal("0.00"),
        "tax_rate_id": None,
        "application_amount": Decimal("50.00"),
        "credit_total": Decimal("50.00"),
        "applies_to_invoice": True,
    }
    kwargs.update(overrides)
    return credits.credit_eligibility_reason(db, **kwargs)


def test_fully_applied_starter_credit_is_eligible() -> None:
    invoice = _invoice()
    assert _reason(_Db(_link(invoice)), invoice=invoice) is None


def test_unlinked_invoice_stays_legacy() -> None:
    reason = _reason(_Db(None))
    assert reason is not None and "Starter-issued" in reason


def test_partial_application_stays_legacy() -> None:
    invoice = _invoice()
    reason = _reason(
        _Db(_link(invoice)),
        invoice=invoice,
        application_amount=Decimal("20.00"),
    )
    assert reason is not None and "partially applied" in reason


def test_manual_number_stays_legacy() -> None:
    invoice = _invoice()
    reason = _reason(_Db(_link(invoice)), invoice=invoice, credit_number="CN-9")
    assert reason is not None and "manually numbered" in reason


def test_taxed_credit_needs_active_tax_rate() -> None:
    invoice = _invoice()
    reason = _reason(
        _Db(_link(invoice)),
        invoice=invoice,
        tax_total=Decimal("7.50"),
    )
    assert reason is not None and "tax rate" in reason

    rate = TaxRate(name="VAT", code="vat_standard", rate=Decimal("0.0750"))
    rate.is_active = True
    assert (
        _reason(
            _Db(_link(invoice), rate),
            invoice=invoice,
            tax_total=Decimal("7.50"),
            tax_rate_id=uuid4(),
        )
        is None
    )
