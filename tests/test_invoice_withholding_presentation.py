"""The same typed WHT values survive customer/admin and PDF presentation."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models.payment_proof import WithholdingTaxStatus
from app.services import billing_invoice_pdf
from app.services.invoice_withholding import InvoiceWithholdingSummary
from app.services.web_invoice_withholding import withholding_display


def _summary() -> InvoiceWithholdingSummary:
    return InvoiceWithholdingSummary(
        invoice_id=uuid4(),
        currency="NGN",
        gross_amount=Decimal("107500"),
        vat_exclusive_amount=Decimal("100000"),
        vat_amount=Decimal("7500"),
        expected_net_amount=Decimal("102500"),
        expected_wht_amount=Decimal("5000"),
        accepted_cash_amount=Decimal("0"),
        accepted_wht_amount=Decimal("0"),
        balance_due=Decimal("107500"),
        record_id=None,
        status=None,
        certificate_reference=None,
        certificate_path=None,
        legacy_gross_payment=False,
        eligible=True,
        ineligibility_reason=None,
        certificate_outstanding_amount=Decimal("0"),
        source_updated_at=None,
    )


def _render(summary: InvoiceWithholdingSummary, *, admin: bool = False) -> str:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
        autoescape=select_autoescape(),
    )
    return environment.get_template("billing/_invoice_withholding.html").render(
        invoice_withholding=summary,
        invoice_withholding_display=withholding_display(None, summary=summary),
        invoice_withholding_admin=admin,
    )


def test_expected_bank_cash_is_separate_from_gross_invoice_and_card_balance() -> None:
    rendered = _render(_summary())
    assert "Expected cash allocation" in rendered
    assert "102,500.00" in rendered
    assert "5,000.00" in rendered
    assert "this estimate is not a bank-transfer instruction" in rendered
    assert "Accepted cash" not in rendered


def test_accepted_evidence_replaces_payment_instructions_and_hides_staff_link() -> None:
    summary = replace(
        _summary(),
        eligible=False,
        record_id=uuid4(),
        status=WithholdingTaxStatus.pending,
        accepted_cash_amount=Decimal("102500"),
        accepted_wht_amount=Decimal("5000"),
        certificate_outstanding_amount=Decimal("5000"),
        balance_due=Decimal("0"),
        certificate_reference="<unsafe>",
    )
    rendered = _render(summary)
    assert "Accepted cash" in rendered
    assert "Expected cash allocation" not in rendered
    assert "Certificate outstanding" in rendered
    assert "&lt;unsafe&gt;" in rendered
    assert "/admin/" not in rendered
    assert "/admin/billing/tax-accounting" in _render(summary, admin=True)


def test_pdf_projection_uses_canonical_accepted_cash_and_wht(monkeypatch) -> None:
    summary = replace(
        _summary(),
        eligible=False,
        record_id=uuid4(),
        status=WithholdingTaxStatus.certified,
        accepted_cash_amount=Decimal("102500"),
        accepted_wht_amount=Decimal("5000"),
    )
    monkeypatch.setattr(
        billing_invoice_pdf, "query_invoice_withholding", lambda db, query: summary
    )
    from app.models.billing import Invoice

    rows = billing_invoice_pdf._invoice_withholding_rows(
        None, Invoice(id=summary.invoice_id)
    )
    assert ("Accepted cash", "₦102,500.00") in rows
    assert ("Accepted withholding tax", "₦5,000.00") in rows
    assert not any(label.startswith("Expected") for label, _ in rows)


def test_unchecked_invoice_has_no_expected_wht_even_when_customer_is_eligible(
    db_session, subscriber, monkeypatch
) -> None:
    from app.models.billing import Invoice, InvoiceStatus
    from app.models.customer_tax_policy import CustomerTaxPolicy
    from app.services import invoice_withholding as owner

    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100000"),
        tax_total=Decimal("7500"),
        total=Decimal("107500"),
        balance_due=Decimal("107500"),
        withholding_tax_selected=False,
    )
    db_session.add_all(
        [
            invoice,
            CustomerTaxPolicy(
                account_id=subscriber.id, withholding_tax_enabled=True, version=1
            ),
        ]
    )
    db_session.commit()
    summary = owner.query_invoice_withholding(
        db_session, owner.InvoiceWithholdingQuery(invoice.id)
    )
    assert summary.expected_net_amount is None
    assert summary.expected_wht_amount is None
    assert not summary.eligible
    assert "Expected cash allocation" not in _render(summary)
    assert billing_invoice_pdf._invoice_withholding_rows(db_session, invoice) == ()


def test_native_discharge_displays_accepted_cash_without_legacy_record(monkeypatch):
    summary = replace(
        _summary(),
        eligible=False,
        record_id=None,
        native_discharge_id=uuid4(),
        status=WithholdingTaxStatus.pending,
        accepted_cash_amount=Decimal("102500"),
        accepted_wht_amount=Decimal("5000"),
        balance_due=Decimal("0"),
        certificate_outstanding_amount=Decimal("5000"),
    )
    rendered = _render(summary, admin=True)
    assert "Accepted cash" in rendered
    assert "102,500.00" in rendered
    assert "Accepted withholding tax" in rendered
    assert "/admin/billing/tax-accounting" not in rendered
    monkeypatch.setattr(
        billing_invoice_pdf, "query_invoice_withholding", lambda db, query: summary
    )
    from app.models.billing import Invoice

    rows = billing_invoice_pdf._invoice_withholding_rows(
        None, Invoice(id=summary.invoice_id)
    )
    assert ("Accepted cash", "₦102,500.00") in rows
    assert ("Accepted withholding tax", "₦5,000.00") in rows
