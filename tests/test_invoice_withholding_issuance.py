"""New WHT orchestration refuses unqualified authority before issuing money."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.billing import Invoice, InvoiceStatus, LedgerEntry, Payment
from app.services import invoice_withholding as withholding
from app.services import invoice_withholding_issuance as owner
from app.services.billing import starter_issuance
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _draft(db, subscriber):
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100000"),
        tax_total=Decimal("7500"),
        total=Decimal("107500"),
        balance_due=Decimal("107500"),
        withholding_tax_selected=True,
        withholding_tax_selection_version=1,
    )
    db.add(invoice)
    db.commit()
    return invoice


def test_native_issue_requires_existing_issuance_lane(
    db_session, subscriber, monkeypatch
):
    invoice = _draft(db_session, subscriber)
    monkeypatch.setattr(
        starter_issuance, "eligibility_reason", lambda db, invoice, **kwargs: "live_off"
    )
    with pytest.raises(owner.InvoiceWithholdingIssuanceError) as caught:
        owner.preview_invoice_withholding_issue(
            db_session, withholding.InvoiceWithholdingQuery(invoice.id)
        )
    assert caught.value.code.endswith("native_issuance_required")
    assert invoice.status == InvoiceStatus.draft
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_issue_requires_real_operator_and_claim_before_financial_participants(
    db_session, subscriber
):
    invoice_id = _draft(db_session, subscriber).id
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(owner.InvoiceWithholdingIssuanceError) as caught:
        owner.confirm_invoice_withholding_issue(
            db_session,
            owner.InvoiceWithholdingIssueConfirm(
                invoice_id, "untrusted-preview", "claim"
            ),
            context=CommandContext.system(
                actor="worker",
                scope="billing:invoice:update",
                reason="Reject missing user authority",
            ),
        )
    assert caught.value.code.endswith("invalid_authority")
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.draft
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(owner.InvoiceWithholdingIssuanceError) as caught:
        owner.confirm_invoice_withholding_issue(
            db_session,
            owner.InvoiceWithholdingIssueConfirm(invoice_id, "untrusted-preview", " "),
            context=CommandContext.system(
                actor="user:operator",
                scope="billing:invoice:update",
                reason="Reject missing supporting claim",
            ),
        )
    assert caught.value.code.endswith("invalid_evidence_reference")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
