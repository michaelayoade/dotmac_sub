from __future__ import annotations

from decimal import Decimal

from app.models.billing import Invoice, InvoiceLine
from app.services import customer_tax_policies, settings_spec
from app.services.billing.invoices import (
    invoice_tax_presentation,
    snapshot_invoice_withholding_tax_for_issuance,
)


def test_issuance_snapshot_uses_customer_policy_and_configured_rate(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setattr(
        customer_tax_policies,
        "get_customer_withholding_tax_policy",
        lambda *_args, **_kwargs: customer_tax_policies.CustomerWithholdingTaxPolicy(
            account_id=subscriber.id,
            withholding_tax_enabled=True,
            version=4,
            updated_by="finance-test",
            updated_at=None,
        ),
    )
    monkeypatch.setattr(
        settings_spec,
        "resolve_value",
        lambda *_args, **_kwargs: "5.00",
    )
    invoice = Invoice(
        account_id=subscriber.id,
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        balance_due=Decimal("107500.00"),
    )

    snapshot_invoice_withholding_tax_for_issuance(db_session, invoice)

    assert invoice.withholding_tax_rate_percent == Decimal("5.00")
    assert invoice.withholding_tax_amount == Decimal("5000.00")
    assert invoice.withholding_tax_policy_version == 4
    assert invoice.net_amount_payable == Decimal("102500.00")


def test_tax_presentation_uses_invoice_snapshots_only(subscriber) -> None:
    invoice = Invoice(
        account_id=subscriber.id,
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        balance_due=Decimal("107500.00"),
        withholding_tax_rate_percent=Decimal("5.00"),
        withholding_tax_amount=Decimal("5000.00"),
        withholding_tax_policy_version=4,
        net_amount_payable=Decimal("102500.00"),
    )
    invoice.lines = [
        InvoiceLine(
            description="Service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100000.00"),
            amount=Decimal("100000.00"),
            tax_rate_percent_snapshot=Decimal("7.5000"),
            is_active=True,
        )
    ]

    presentation = invoice_tax_presentation(invoice)

    assert presentation.vat_label == "VAT (7.5%)"
    assert presentation.wht_label == "WHT (5%)"
    assert presentation.withholding_tax_amount == Decimal("5000.00")
    assert presentation.net_amount_payable == Decimal("102500.00")
