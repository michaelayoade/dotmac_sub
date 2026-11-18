"""The existing Issue adapter reviews and delegates typed WHT commands."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_kernel.money import Currency, Money
from dotmac_tax.contracts import TaxFact, TaxPolicyPreviewV1

from app.services import invoice_withholding as withholding
from app.services import invoice_withholding_issuance as issuance
from app.services import web_invoice_withholding as presentation
from app.services.db_session_adapter import db_session_adapter
from app.web.admin import billing_invoice_actions as routes


def _selection(monkeypatch, invoice_id: UUID, *, selected: bool = True):
    result = withholding.InvoiceWithholdingSelection(
        invoice_id, selected, 1, True, None, True
    )
    monkeypatch.setattr(
        withholding, "query_invoice_withholding_selection", lambda db, query: result
    )


def _preview(invoice_id: UUID) -> issuance.InvoiceWithholdingIssuePreview:
    currency = Currency("NGN", 2)
    account_id, jurisdiction_id, code_id = uuid4(), uuid4(), uuid4()
    base = Money(Decimal("100000"), currency)
    tax = Money(Decimal("5000"), currency)
    fact = TaxFact(
        jurisdiction_id=jurisdiction_id,
        occurred_on=datetime.now(UTC).date(),
        fact_kind="invoice",
        recognition_basis_code="invoice_issued",
        transaction_side="withholding",
        base_amount=base,
        source_ref=f"invoice:{invoice_id}",
        source_version="1",
        evidence_ref="fixture:published-policy",
    )
    tax_preview = TaxPolicyPreviewV1(
        tenant_id=uuid4(),
        jurisdiction_id=jurisdiction_id,
        occurred_on=fact.occurred_on,
        source_amount=base,
        net_amount=base,
        tax_amount=tax,
        gross_amount=Money(Decimal("105000"), currency),
        components=(),
    )
    snapshot = withholding.InvoiceWithholdingSnapshot(
        invoice_id=invoice_id,
        account_id=uuid4(),
        currency="NGN",
        gross_amount=Decimal("107500"),
        net_amount=Decimal("102500"),
        wht_amount=Decimal("5000"),
        wht_rate=Decimal("5"),
        vat_exclusive_amount=Decimal("100000"),
        vat_amount=Decimal("7500"),
        policy_version=1,
        billing_account_id=account_id,
        tax_fact=fact,
        tax_preview=tax_preview,
        tax_code_id=code_id,
    )
    cash = Money(Decimal("102500"), currency)
    funding = issuance.InvoiceWithholdingFunding(
        account_id, 1, cash, cash, "cash-state"
    )
    return issuance.InvoiceWithholdingIssuePreview(snapshot, funding, "f" * 64)


def test_existing_issue_endpoint_confirms_stored_selection_with_canonical_owner(
    monkeypatch,
):
    invoice_id = uuid4()
    _selection(monkeypatch, invoice_id)
    db = object()
    captured = []
    monkeypatch.setattr(routes, "has_permission", lambda *args: True)
    monkeypatch.setattr(
        db_session_adapter,
        "release_read_transaction",
        lambda db: captured.append("read-ended"),
    )
    monkeypatch.setattr(
        issuance,
        "confirm_invoice_withholding_issue",
        lambda db, command, *, context: captured.append((command, context)),
    )
    response = routes.invoice_issue_from_detail(
        invoice_id,
        request=None,
        preview_fingerprint="a" * 64,
        confirmed="yes",
        evidence_reference="WHT-DECLARATION",
        idempotency_token="one-confirm-token",
        auth={"principal_id": "operator", "principal_type": "user"},
        db=db,
    )
    assert response.status_code == 303
    assert captured[0] == "read-ended"
    command, context = captured[1]
    assert command == issuance.InvoiceWithholdingIssueConfirm(
        invoice_id, "a" * 64, "WHT-DECLARATION"
    )
    assert context.actor == "user:operator"
    assert context.scope == "billing:invoice:update"


@pytest.mark.parametrize("confirmed", [None, "", "no", "true"])
def test_selected_withholding_cannot_issue_without_explicit_confirmation(
    monkeypatch, confirmed
):
    invoice_id = uuid4()
    _selection(monkeypatch, invoice_id)
    monkeypatch.setattr(routes, "has_permission", lambda *args: True)
    monkeypatch.setattr(
        issuance,
        "confirm_invoice_withholding_issue",
        lambda *args, **kwargs: pytest.fail("Unconfirmed review must not issue"),
    )
    response = routes.invoice_issue_from_detail(
        invoice_id,
        request=None,
        preview_fingerprint="a" * 64,
        confirmed=confirmed,
        evidence_reference="WHT-DECLARATION",
        idempotency_token="one-confirm-token",
        auth={"principal_id": "operator", "principal_type": "user"},
        db=object(),
    )
    assert response.status_code == 303
    assert "Review+and+confirm" in response.headers["location"]


def test_confirmation_cannot_bypass_checkbox_changed_since_preview(monkeypatch):
    invoice_id = uuid4()
    _selection(monkeypatch, invoice_id, selected=False)
    monkeypatch.setattr(
        issuance,
        "confirm_invoice_withholding_issue",
        lambda *args, **kwargs: pytest.fail("Changed selection must not settle"),
    )
    response = routes.invoice_issue_from_detail(
        invoice_id,
        request=None,
        preview_fingerprint="a" * 64,
        auth={"principal_id": "operator"},
        db=object(),
    )
    assert response.status_code == 303
    assert "selection+changed" in response.headers["location"]


def test_selected_withholding_requires_cash_settlement_permission(monkeypatch):
    invoice_id = uuid4()
    _selection(monkeypatch, invoice_id)
    monkeypatch.setattr(routes, "has_permission", lambda *args: False)
    response = routes.invoice_issue_from_detail(
        invoice_id, request=None, auth={"principal_id": "operator"}, db=object()
    )
    assert response.status_code == 303
    assert "Payment+permission" in response.headers["location"]


def test_existing_issue_button_first_reviews_selected_withholding_without_issuing(
    monkeypatch,
):
    from app.web import admin

    invoice_id = uuid4()
    _selection(monkeypatch, invoice_id)
    preview = _preview(invoice_id)
    monkeypatch.setattr(routes, "has_permission", lambda *args: True)
    monkeypatch.setattr(
        issuance, "preview_invoice_withholding_issue", lambda db, query: preview
    )
    monkeypatch.setattr(
        issuance,
        "confirm_invoice_withholding_issue",
        lambda *args, **kwargs: pytest.fail("Review cannot issue"),
    )
    monkeypatch.setattr(
        presentation,
        "invoice_identity",
        lambda db, query: presentation.InvoiceWithholdingIdentity("INV-1", "Customer"),
    )
    monkeypatch.setattr(
        admin, "get_current_user", lambda request: {"principal_id": "operator"}
    )
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    monkeypatch.setattr(
        routes.templates,
        "TemplateResponse",
        lambda template, context: (template, context),
    )
    template, context = routes.invoice_issue_from_detail(
        invoice_id, request=None, auth={"principal_id": "operator"}, db=object()
    )
    assert template == "admin/billing/withholding_issue.html"
    assert context["preview"] is preview
    assert context["invoice_id"] == invoice_id
    action = context["issue_action_form"]
    assert action.action_url == f"/admin/billing/invoices/{invoice_id}/issue"
    assert action.confirmation is not None
    assert action.submit_label == "Issue invoice"
    assert "102,500.00" in action.impact
    assert "5,000.00" in action.impact


@pytest.mark.parametrize("action", ["preview", "confirm"])
def test_retired_payment_withholding_cannot_bypass_invoice_issue_flow(
    monkeypatch, action
):
    from fastapi import HTTPException

    from app.web.admin import billing_payments

    monkeypatch.setattr(
        billing_payments.web_billing_payments_service,
        "preview_payment_create",
        lambda *args, **kwargs: pytest.fail("Retired WHT input must not prepare cash"),
    )
    monkeypatch.setattr(
        billing_payments.web_billing_payments_service,
        "process_payment_create_with_audit",
        lambda *args, **kwargs: pytest.fail("Retired WHT input must not create cash"),
    )
    route = (
        billing_payments.payment_create_preview
        if action == "preview"
        else billing_payments.payment_create
    )
    with pytest.raises(HTTPException) as refused:
        route(request=None, amount="102500", withholding_tax=True, db=object())
    assert refused.value.status_code == 410
    assert "Select withholding on the invoice" in refused.value.detail
