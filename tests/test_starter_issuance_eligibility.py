"""Starter issuance eligibility (Slice 6a): cheap refusals stay legacy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, Subscription
from app.models.subscriber import Subscriber
from app.services.billing import starter_issuance as issuance
from app.services.billing import starter_policies
from app.services.billing.account_credit import AccountCreditApplications

_POLICY_DEFAULTS = {
    "starter_billing_account_ref_template": "subscriber:{subscriber_id}",
    "starter_billing_source_system": "dotmac-sub",
    "starter_billing_admin_source_kind": "admin_manual_invoice_line",
    "starter_billing_admin_contract_line_template": "admin-manual:{line_id}",
    "starter_billing_admin_contract_version": "admin-manual-v1",
    "starter_billing_admin_price_version": "admin-manual-v1",
    "starter_billing_admin_fx_observation_template": "admin-manual-fx:{invoice_id}",
    "starter_billing_recurring_source_kind": "scheduled_billing_invoice_line",
    "starter_billing_recurring_contract_line_template": "scheduled-billing:{line_id}",
    "starter_billing_recurring_contract_version": "scheduled-billing-v1",
    "starter_billing_recurring_price_version": "scheduled-billing-v1",
    "starter_billing_recurring_source_fact_template": "scheduled-billing-invoice:{invoice_id}",
    "starter_billing_recurring_line_source_fact_template": "scheduled-billing-invoice-line:{line_id}",
    "starter_billing_recurring_fx_observation_template": "scheduled-billing-fx:{invoice_id}",
    "starter_billing_system_actor_ref": "system:billing_automation",
    "starter_billing_default_minor_units": 2,
    "starter_billing_subject_party_template": "party:{party_id}",
    "starter_billing_subject_subscriber_template": "subscriber:{subscriber_id}",
    "starter_billing_service_subscription_template": "subscription:{subscription_id}",
    "starter_billing_service_subscriber_template": "subscriber:{subscriber_id}",
    "starter_billing_source_fact_version": "1",
    "starter_billing_fx_observation_version": "1",
    "starter_billing_fx_rate_purpose": "document_currency",
    "starter_billing_fx_rounding_policy": "minor_units",
    "starter_billing_fx_provenance": "source-supplied",
    "starter_billing_payment_method_code": "bank_transfer",
    "starter_billing_correlation_template": "starter-issuance:{invoice_id}",
    "starter_billing_due_evidence_ref_template": "invoice-draft:{invoice_id}",
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
    "starter_billing_admin_line_source_fact_template": "admin-manual-invoice-line:{line_id}",
    "starter_billing_invoice_series_prefix": "INV",
    "starter_billing_credit_note_series_prefix": "CN",
    "starter_billing_default_country_code": "NGA",
    "starter_billing_series_start_value": 1,
}


class _Db:
    def __init__(
        self,
        subscriber: Subscriber | None,
        subscription: Subscription | None = None,
    ) -> None:
        self._subscriber = subscriber
        self._subscription = subscription

    def get(self, model, _id):  # noqa: ANN001, ANN202
        if model is Subscription:
            return self._subscription
        return self._subscriber


@pytest.fixture(autouse=True)
def _policy_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        starter_policies,
        "resolve_value",
        lambda _db, _domain, key: _POLICY_DEFAULTS[key],
    )


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


def test_discount_and_unreconciled_quantity_stay_legacy() -> None:
    discounted = issuance.eligibility_reason(
        _Db(_subscriber()),
        _invoice(discount_amount=Decimal("5.00")),  # type: ignore[arg-type]
    )
    assert discounted is not None and "discounted" in discounted
    invoice = _invoice()
    invoice.lines[0].quantity = Decimal("2")  # type: ignore[attr-defined]
    quantity = issuance.eligibility_reason(_Db(_subscriber()), invoice)  # type: ignore[arg-type]
    assert quantity is not None and "reconcile" in quantity


def _multiline_invoice(**overrides) -> Invoice:
    invoice = _invoice(
        subtotal=Decimal("150.00"),
        total=Decimal("150.00"),
        balance_due=Decimal("150.00"),
        **overrides,
    )
    invoice.lines = [  # type: ignore[attr-defined]
        InvoiceLine(
            description="Access",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_active=True,
        ),
        InvoiceLine(
            description="Router rental",
            quantity=Decimal("2"),
            unit_price=Decimal("25.00"),
            amount=Decimal("50.00"),
            is_active=True,
        ),
    ]
    return invoice


def test_heterogeneous_multiline_stays_legacy() -> None:
    invoice = _multiline_invoice()
    invoice.lines[1].subscription_id = uuid4()  # type: ignore[attr-defined]
    reason = issuance.eligibility_reason(_Db(_subscriber()), invoice)  # type: ignore[arg-type]
    assert reason is not None and "multiple services" in reason


def test_zero_amount_line_stays_legacy() -> None:
    invoice = _multiline_invoice()
    invoice.lines[1].quantity = Decimal("1")  # type: ignore[attr-defined]
    invoice.lines[1].unit_price = Decimal("0.00")  # type: ignore[attr-defined]
    invoice.lines[1].amount = Decimal("0.00")  # type: ignore[attr-defined]
    reason = issuance.eligibility_reason(_Db(_subscriber()), invoice)  # type: ignore[arg-type]
    assert reason is not None and "non-positive" in reason


def test_homogeneous_multiline_is_eligible(monkeypatch) -> None:
    monkeypatch.setattr(
        starter_policies,
        "resolve_value",
        lambda _db, _domain, key: _POLICY_DEFAULTS[key],
    )
    monkeypatch.setattr(issuance, "_seller_snapshot", lambda *_a, **_k: object())
    monkeypatch.setattr(
        issuance, "get_invoice_bank_details", lambda *_a, **_k: object()
    )
    reason = issuance.eligibility_reason(_Db(_subscriber()), _multiline_invoice())  # type: ignore[arg-type]
    assert reason is None


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


def _recurring_invoice() -> Invoice:
    invoice = _invoice(invoice_number="INV-1042")
    invoice.lines[0].subscription_id = uuid4()  # type: ignore[attr-defined]
    return invoice


def _subscription(**overrides) -> Subscription:
    subscription = Subscription(billing_mode=BillingMode.postpaid)
    for key, value in overrides.items():
        setattr(subscription, key, value)
    return subscription


def _stub_recurring_evidence(monkeypatch, *, account_credit: Decimal) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(issuance, "_seller_snapshot", lambda *_a, **_k: object())
    monkeypatch.setattr(
        issuance, "get_invoice_bank_details", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(
        AccountCreditApplications,
        "preview_invoice_funding",
        staticmethod(lambda *_a, **_k: SimpleNamespace(account_credit=account_credit)),
    )


def test_system_numbered_draft_is_recurring_eligible(monkeypatch) -> None:
    _stub_recurring_evidence(monkeypatch, account_credit=Decimal("0.00"))
    reason = issuance.recurring_eligibility_reason(
        _Db(_subscriber(), _subscription()),  # type: ignore[arg-type]
        _recurring_invoice(),
    )
    assert reason is None


def test_recurring_with_account_credit_stays_legacy(monkeypatch) -> None:
    _stub_recurring_evidence(monkeypatch, account_credit=Decimal("25.00"))
    reason = issuance.recurring_eligibility_reason(
        _Db(_subscriber(), _subscription()),  # type: ignore[arg-type]
        _recurring_invoice(),
    )
    assert reason is not None and "account credit" in reason


def test_recurring_prepaid_subscription_stays_legacy(monkeypatch) -> None:
    _stub_recurring_evidence(monkeypatch, account_credit=Decimal("0.00"))
    reason = issuance.recurring_eligibility_reason(
        _Db(  # type: ignore[arg-type]
            _subscriber(), _subscription(billing_mode=BillingMode.prepaid)
        ),
        _recurring_invoice(),
    )
    assert reason is not None and "prepaid" in reason


def test_recurring_line_without_subscription_stays_legacy(monkeypatch) -> None:
    _stub_recurring_evidence(monkeypatch, account_credit=Decimal("0.00"))
    invoice = _recurring_invoice()
    invoice.lines[0].subscription_id = None  # type: ignore[attr-defined]
    reason = issuance.recurring_eligibility_reason(
        _Db(_subscriber(), _subscription()),  # type: ignore[arg-type]
        invoice,
    )
    assert reason is not None and "subscription" in reason
