"""Starter issuance eligibility (Slice 6a): cheap refusals stay legacy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services.billing import starter_issuance as issuance
from app.services.billing import starter_policies

_POLICY_DEFAULTS = {
    "starter_billing_account_ref_template": "subscriber:{subscriber_id}",
    "starter_billing_source_system": "dotmac-sub",
    "starter_billing_admin_source_kind": "admin_manual_invoice_line",
    "starter_billing_admin_contract_line_template": "admin-manual:{line_id}",
    "starter_billing_admin_contract_version": "admin-manual-v1",
    "starter_billing_admin_price_version": "admin-manual-v1",
    "starter_billing_default_charge_component": "other",
    "starter_billing_tax_jurisdiction": "NG",
    "starter_billing_tax_policy_id": "sub-manual-vat",
    "starter_billing_tax_policy_version": "1",
    "starter_billing_due_terms_version": "1",
    "starter_billing_due_derivation_policy": "calendar_days",
    "starter_billing_due_derivation_version": "1",
    "starter_billing_invoice_series": "invoice",
    "starter_billing_credit_note_series": "credit-note",
    "starter_billing_document_profile_code": "invoice.standard",
    "starter_billing_document_profile_version": "1",
    "starter_billing_credit_note_profile_code": "credit_note.standard",
    "starter_billing_default_locale": "en-NG",
    "starter_billing_default_timezone": "Africa/Lagos",
    "starter_billing_default_unit_code": "unit",
    "starter_payments_accepted_evidence": "dotmac-payments:confirmed-cash-v1",
    "starter_billing_admin_source_fact_template": "admin-manual-invoice:{invoice_id}",
    "starter_billing_invoice_series_prefix": "INV",
    "starter_billing_credit_note_series_prefix": "CN",
    "starter_billing_default_country_code": "NGA",
    "starter_billing_series_start_value": 1,
}


class _Db:
    def __init__(self, subscriber: Subscriber | None) -> None:
        self._subscriber = subscriber

    def get(self, _model, _id):  # noqa: ANN001, ANN202
        return self._subscriber


def _invoice(**overrides) -> Invoice:
    invoice = Invoice(
        account_id=uuid4(),
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        discount_amount=Decimal("0.00"),
        due_at=datetime(2026, 11, 16, tzinfo=UTC),
        is_proforma=False,
    )
    line = InvoiceLine(
        description="Access",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        amount=Decimal("100.00"),
        is_active=True,
    )
    invoice.lines = [line]  # type: ignore[attr-defined]
    for key, value in overrides.items():
        setattr(invoice, key, value)
    return invoice


def _subscriber(**overrides) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.test",
        billing_mode=BillingMode.postpaid,
        billing_address_line1="1 Proof Street",
        billing_city="Abuja",
    )
    for key, value in overrides.items():
        setattr(subscriber, key, value)
    return subscriber


def test_proforma_stays_legacy() -> None:
    reason = issuance.eligibility_reason(
        _Db(_subscriber()),
        _invoice(is_proforma=True),  # type: ignore[arg-type]
    )
    assert reason is not None and "proforma" in reason


def test_manual_number_stays_legacy() -> None:
    reason = issuance.eligibility_reason(
        _Db(_subscriber()),
        _invoice(invoice_number="INV-LEGACY-1"),  # type: ignore[arg-type]
    )
    assert reason is not None and "manually numbered" in reason


def test_discount_and_quantity_stay_legacy() -> None:
    discounted = issuance.eligibility_reason(
        _Db(_subscriber()),
        _invoice(discount_amount=Decimal("5.00")),  # type: ignore[arg-type]
    )
    assert discounted is not None and "discounted" in discounted
    invoice = _invoice()
    invoice.lines[0].quantity = Decimal("2")  # type: ignore[attr-defined]
    quantity = issuance.eligibility_reason(_Db(_subscriber()), invoice)  # type: ignore[arg-type]
    assert quantity is not None and "quantity" in quantity


def test_prepaid_stays_legacy() -> None:
    reason = issuance.eligibility_reason(
        _Db(_subscriber(billing_mode=BillingMode.prepaid)),
        _invoice(),  # type: ignore[arg-type]
    )
    assert reason is not None and "prepaid" in reason


def test_missing_customer_address_stays_legacy(monkeypatch) -> None:
    monkeypatch.setattr(
        starter_policies,
        "resolve_value",
        lambda _db, _domain, key: _POLICY_DEFAULTS[key],
    )
    monkeypatch.setattr(issuance, "_seller_snapshot", lambda *_a, **_k: object())
    reason = issuance.eligibility_reason(
        _Db(_subscriber(billing_address_line1=None, address_line1=None)),
        _invoice(),  # type: ignore[arg-type]
    )
    assert reason is not None and "customer" in reason
