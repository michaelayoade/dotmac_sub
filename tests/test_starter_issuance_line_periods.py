"""Exact line dates survive the Sub-to-Billing issuance boundary."""

from datetime import UTC, datetime

import pytest
from dotmac_billing.contracts import ServicePeriodStatus

from app.models.billing import Invoice, InvoiceLine
from app.services.billing.starter_issuance import StarterIssuanceError, _service_period


def test_line_period_overrides_document_header_without_changing_dates():
    invoice = Invoice(
        billing_period_start=datetime(2027, 1, 1, tzinfo=UTC),
        billing_period_end=datetime(2027, 3, 1, tzinfo=UTC),
    )
    line = InvoiceLine(
        metadata_={
            "billing_period_start": "2027-01-02T00:00:00+01:00",
            "billing_period_end": "2027-02-02T00:00:00+01:00",
        }
    )
    period = _service_period(invoice, line)
    assert period.status is ServicePeriodStatus.VERIFIED
    assert period.starts_at.isoformat() == "2027-01-02T00:00:00+01:00"
    assert period.ends_at.isoformat() == "2027-02-02T00:00:00+01:00"


def test_legacy_line_without_metadata_retains_existing_header_period():
    invoice = Invoice(
        billing_period_start=datetime(2027, 1, 2, tzinfo=UTC),
        billing_period_end=datetime(2027, 2, 2, tzinfo=UTC),
    )
    period = _service_period(invoice, InvoiceLine())
    assert period.starts_at == invoice.billing_period_start
    assert period.ends_at == invoice.billing_period_end


def test_missing_period_stays_unknown_without_inventing_a_month():
    period = _service_period(Invoice(), InvoiceLine())
    assert period.status is ServicePeriodStatus.UNKNOWN_UNVERIFIED
    assert period.starts_at is None and period.ends_at is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"billing_period_start": "2027-01-02T00:00:00+00:00"},
        {"billing_period_start": "2027-01-02", "billing_period_end": "2027-02-02"},
        {
            "billing_period_start": "2027-02-02T00:00:00+00:00",
            "billing_period_end": "2027-01-02T00:00:00+00:00",
        },
    ],
)
def test_partial_invalid_or_naive_line_evidence_refuses_instead_of_header_fallback(
    metadata,
):
    with pytest.raises(StarterIssuanceError):
        _service_period(Invoice(), InvoiceLine(metadata_=metadata))


def test_missing_candidate_billing_api_refuses_without_legacy_fallback(monkeypatch):
    import builtins

    from app.services.billing import starter_issuance

    original_import = builtins.__import__

    def without_candidate(name, globals=None, locals=None, fromlist=(), level=0):
        if (
            name == "dotmac_billing.commands"
            and "CreateMultiPeriodDraftDocumentV1" in fromlist
        ):
            raise ImportError("released a8 has no candidate command")
        return original_import(name, globals, locals, fromlist, level)

    def forbidden_fallback(*args, **kwargs):
        raise AssertionError("legacy author must not handle a multi-period refusal")

    monkeypatch.setattr(builtins, "__import__", without_candidate)
    monkeypatch.setattr(
        starter_issuance, "create_multi_line_draft_document", forbidden_fallback
    )
    with pytest.raises(StarterIssuanceError) as failure:
        starter_issuance._create_period_document(
            None,
            command=None,
            multi_period=True,  # type: ignore[arg-type]
        )
    assert failure.value.code.endswith("multi_period_billing_unavailable")


def test_inclusive_monthly_line_projects_net_and_vat_once():
    from decimal import Decimal

    from app.models.billing import TaxApplication
    from app.services.billing.starter_issuance import (
        _line_net_unit_amount,
        _line_pre_tax_amount,
        _line_tax_amount,
    )

    line = InvoiceLine(
        quantity=Decimal("1"),
        unit_price=Decimal("107.50"),
        amount=Decimal("107.50"),
        tax_application=TaxApplication.inclusive,
        tax_rate_code_snapshot="VAT",
        tax_rate_percent_snapshot=Decimal("7.5"),
    )
    assert _line_tax_amount(line, 2) == Decimal("7.50")
    assert _line_pre_tax_amount(line, 2) == Decimal("100.00")
    assert _line_net_unit_amount(line, 2) == Decimal("100.00")


def test_catalogue_one_time_has_no_invented_subscription_period():
    line = InvoiceLine(
        metadata_={"future_catalogue_invoice": True, "kind": "catalogue_one_time"}
    )
    invoice = Invoice(
        billing_period_start=datetime(2027, 1, 2, tzinfo=UTC),
        billing_period_end=datetime(2027, 2, 2, tzinfo=UTC),
    )
    period = _service_period(invoice, line)
    assert period.status is ServicePeriodStatus.NOT_APPLICABLE
    assert period.starts_at is None and period.ends_at is None


def test_inclusive_catalogue_months_reconcile_for_starter_eligibility(monkeypatch):
    from decimal import Decimal

    from app.models.billing import TaxApplication
    from app.services.billing import starter_issuance, starter_policies
    from tests.test_starter_issuance_eligibility import (
        _POLICY_DEFAULTS,
        _Db,
        _multiline_invoice,
        _subscriber,
    )

    monkeypatch.setattr(
        starter_issuance, "starter_ledger_live_enabled", lambda _db: True
    )
    monkeypatch.setattr(
        starter_policies,
        "resolve_value",
        lambda _db, _domain, key: _POLICY_DEFAULTS[key],
    )
    monkeypatch.setattr(
        starter_issuance, "_seller_snapshot", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(
        starter_issuance, "get_invoice_bank_details", lambda *_a, **_k: object()
    )
    invoice = _multiline_invoice()
    invoice.subtotal = Decimal("200")
    invoice.tax_total = Decimal("15")
    invoice.total = Decimal("215")
    for line in invoice.lines:
        line.quantity = Decimal("1")
        line.unit_price = line.amount = Decimal("107.50")
        line.tax_application = TaxApplication.inclusive
        line.tax_rate_percent_snapshot = Decimal("7.5")
        line.tax_rate_code_snapshot = "VAT"
        line.metadata_ = {"future_catalogue_invoice": True}
    assert starter_issuance.eligibility_reason(_Db(_subscriber()), invoice) is None  # type: ignore[arg-type]


def test_catalogue_subscription_timing_cannot_be_misrepresented_as_account_timing(
    monkeypatch,
):
    from uuid import uuid4

    from app.models.catalog import BillingMode
    from app.services.billing import starter_issuance, starter_policies
    from tests.test_starter_issuance_eligibility import (
        _POLICY_DEFAULTS,
        _Db,
        _invoice,
        _subscriber,
        _subscription,
    )

    monkeypatch.setattr(
        starter_issuance, "starter_ledger_live_enabled", lambda _db: True
    )
    monkeypatch.setattr(
        starter_policies,
        "resolve_value",
        lambda _db, _domain, key: _POLICY_DEFAULTS[key],
    )
    invoice = _invoice()
    invoice.lines[0].subscription_id = uuid4()
    invoice.lines[0].metadata_ = {"future_catalogue_invoice": True}
    subscription = _subscription(
        subscriber_id=invoice.account_id, billing_mode=BillingMode.prepaid
    )
    reason = starter_issuance.eligibility_reason(
        _Db(_subscriber(), subscription), invoice
    )  # type: ignore[arg-type]
    assert reason is not None and "collection timing" in reason


@pytest.mark.parametrize("lane", ["admin", "recurring", "prepaid"])
def test_issue_rechecks_refreshed_state_after_waiting_for_account_fence(
    monkeypatch, lane
):
    from decimal import Decimal

    from app.services.billing import starter_issuance

    invoice = Invoice(discount_amount=Decimal("0"))
    observed = []

    def concurrent_edit_becomes_visible(_db, current):
        current.discount_amount = Decimal("5")
        observed.append("refreshed")

    def eligibility(_db, current, **kwargs):
        observed.append(current.discount_amount)
        return "discounted invoice is not eligible" if current.discount_amount else None

    def forbid_write(*args, **kwargs):
        raise AssertionError(
            "stale pre-lock eligibility must not authorize a financial write"
        )

    monkeypatch.setattr(
        starter_issuance,
        "_lock_and_refresh_issue_evidence",
        concurrent_edit_becomes_visible,
    )
    monkeypatch.setattr(starter_issuance, "_issue_via_starter", forbid_write)
    with pytest.raises(StarterIssuanceError) as refused:
        if lane == "admin":
            monkeypatch.setattr(starter_issuance, "eligibility_reason", eligibility)
            starter_issuance.issue_admin_invoice_via_starter(
                None, invoice=invoice, actor_ref="staff:test"
            )  # type: ignore[arg-type]
        elif lane == "recurring":
            monkeypatch.setattr(
                starter_issuance, "recurring_eligibility_reason", eligibility
            )
            starter_issuance.issue_recurring_invoice_via_starter(
                None, invoice=invoice, issued_at=datetime.now(UTC)
            )  # type: ignore[arg-type]
        else:
            monkeypatch.setattr(
                starter_issuance, "prepaid_eligibility_reason", eligibility
            )
            starter_issuance.issue_prepaid_invoice_via_starter(
                None,
                invoice=invoice,
                issued_at=datetime.now(UTC),
                billing_line_key="period:test",
            )  # type: ignore[arg-type]
    assert refused.value.code.endswith("ineligible")
    assert observed == ["refreshed", Decimal("5")]
