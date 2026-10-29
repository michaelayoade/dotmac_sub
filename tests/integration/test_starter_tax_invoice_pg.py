"""Native issuance and immutable Tax presentation on migrated PostgreSQL.

Pending Billing a9 dependency publication; no local execution is authorized.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import InvoiceDocumentFactV1
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from sqlalchemy import select

from app.models.billing import InvoiceStatus, StarterInvoiceLink
from app.models.catalog import BillingMode
from app.services import direct_transfer_intents
from app.services.billing import starter_issuance, starter_positions
from app.services.billing.starter_settlement import settle_starter_invoice_payment
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration.test_starter_issuance_switch_pg import (
    _draft,
    _seller_and_bank,  # noqa: F401 -- shared pytest fixture
    _starter_series_isolation,  # noqa: F401 -- shared pytest fixture
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("_seller_and_bank", "_starter_series_isolation"),
]


def _issue(db_session, subscriber):
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    invoice = _draft(db_session, subscriber)
    link = starter_issuance.issue_admin_invoice_via_starter(
        db_session, invoice=invoice, actor_ref="user:native-tax-proof"
    )
    db_session.flush()
    return invoice, link


def test_missing_policy_refuses_after_eligibility_without_legacy_fallback(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(starter_issuance, "resolve_value", lambda *_args: None)
    with pytest.raises(
        starter_issuance.StarterIssuanceError, match="policy is missing"
    ):
        _issue(db_session, subscriber)
    assert db_session.scalar(select(StarterInvoiceLink)) is None


def test_issuance_freezes_tax_and_policy_edit_does_not_change_document(
    db_session, subscriber, starter_native_tax_policy
):
    binding = starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, link = _issue(db_session, subscriber)
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert fact is not None
    assert len(fact.tax_evidence) == 2
    assert {item.purpose for item in fact.tax_evidence} == {
        "invoice_tax",
        "expected_withholding",
    }
    assert all(
        item.determination.source_fingerprint and item.determination.result_fingerprint
        for item in fact.tax_evidence
    )
    assert fact.expected_withholding.amount == Decimal("5.00")
    assert invoice.balance_due == Decimal("107.50")
    legacy = InvoiceDocumentFactV1(
        **{
            field.name: getattr(fact, field.name)
            for field in fields(InvoiceDocumentFactV1)
        }
    )
    assert (
        starter_positions.apply_fact_overlay(invoice, legacy).tax_presentation is None
    )
    with pytest.raises(DomainError) as refusal:
        direct_transfer_intents._invoice_withholding_tax_snapshot(
            db_session, account_id=subscriber.id, invoice=invoice
        )
    assert refusal.value.code.endswith("native_withholding_proof_contract_pending")
    binding["invoice_tax"]["jurisdiction_id"] = str(uuid4())
    assert starter_positions.starter_invoice_fact(db_session, invoice) == fact

    now = datetime.now(UTC)
    scope = TenantScope(OPERATOR_TENANT_ID)
    reference = f"native-tax-payment:{uuid4()}"
    open_payment_intent(
        db_session,
        scope=scope,
        command=OpenPaymentIntent(
            reference=reference,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(invoice.id),
            requested=Money.of("107.50", Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=now,
            expires_at=now + timedelta(days=1),
        ),
    )
    db_session.flush()
    settle_starter_invoice_payment(
        db_session,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=f"native-tax-payment:{uuid4()}",
        confirmed_amount=Decimal("107.50"),
        occurred_at=now,
        observed_at=now,
    )
    db_session.flush()
    view = starter_positions.apply_fact_overlay(invoice, fact)
    assert invoice.status is InvoiceStatus.paid
    assert view.balance_due == 0
    assert view.tax_presentation.original_expected_net == Decimal("102.50")
    with pytest.raises(DomainError) as refusal:
        direct_transfer_intents._invoice_withholding_tax_snapshot(
            db_session, account_id=subscriber.id, invoice=invoice
        )
    assert refusal.value.code.endswith("invoice_withholding_tax_basis_unavailable")
    assert view.total == Decimal("107.50")
