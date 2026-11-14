from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.billing import Invoice, InvoiceStatus, LedgerEntry, Payment
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.payment_proof import WithholdingTaxRecord
from app.schemas.billing import InvoiceLineCreate, InvoiceUpdate
from app.services import invoice_withholding as owner
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _draft(db, subscriber, monkeypatch):
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100000"),
        tax_total=Decimal("7500"),
        total=Decimal("107500"),
        balance_due=Decimal("107500"),
    )
    db.add_all(
        [
            invoice,
            CustomerTaxPolicy(
                account_id=subscriber.id, withholding_tax_enabled=True, version=1
            ),
        ]
    )
    db.commit()
    return invoice


def _toggle(db, invoice, selected, version):
    command = owner.SetDraftInvoiceWithholdingSelection(invoice.id, selected, version)
    db_session_adapter.release_read_transaction(db)
    return owner.set_draft_invoice_withholding_selection(
        db,
        command,
        context=CommandContext.system(
            actor="user:test-principal",
            scope="billing:invoice:update",
            reason="Staff chose invoice withholding",
        ),
    )


def test_eligible_customer_does_not_imply_native_policy_or_default_opt_in(
    db_session, subscriber, monkeypatch
):
    invoice = _draft(db_session, subscriber, monkeypatch)
    initial = owner.query_invoice_withholding_selection(
        db_session, owner.InvoiceWithholdingQuery(invoice.id)
    )
    assert not initial.selected and initial.version == 0
    assert not initial.eligible
    with pytest.raises(owner.InvoiceWithholdingError):
        _toggle(db_session, invoice, True, 0)
    assert not invoice.withholding_tax_selected
    assert invoice.status == InvoiceStatus.draft
    assert not list(db_session.scalars(select(Payment)))
    assert not list(db_session.scalars(select(WithholdingTaxRecord)))
    assert not list(db_session.scalars(select(LedgerEntry)))


def test_selected_issued_invoice_cannot_be_unchecked_by_command_or_orm(
    db_session, subscriber, monkeypatch
):
    invoice = _draft(db_session, subscriber, monkeypatch)
    # Existing selected source state; this test qualifies immutability only.
    invoice.withholding_tax_selected = True
    invoice.withholding_tax_selection_version = 1
    permit = owner.InvoiceWithholdingSelectionWritePermit(invoice.id, True, 1)
    db_session.info[owner.INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY] = permit
    db_session.flush()
    db_session.info.pop(owner.INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY, None)
    invoice.status = InvoiceStatus.issued
    db_session.commit()
    with pytest.raises(owner.InvoiceWithholdingError, match="cannot change"):
        _toggle(db_session, invoice, False, 1)
    invoice.withholding_tax_selected = False
    invoice.withholding_tax_selection_version = 2
    with pytest.raises(ValueError, match="immutable"):
        db_session.flush()
    db_session.rollback()
    assert db_session.get(Invoice, invoice.id).withholding_tax_selected


def test_direct_draft_orm_and_generic_api_cannot_set_selection(
    db_session, subscriber, monkeypatch
):
    invoice = _draft(db_session, subscriber, monkeypatch)
    invoice.withholding_tax_selected = True
    invoice.withholding_tax_selection_version = 1
    with pytest.raises(ValueError, match="draft selection owner"):
        db_session.flush()
    db_session.rollback()
    with pytest.raises(ValidationError, match="dedicated draft selection"):
        InvoiceUpdate.model_validate({"metadata": {"withholding_tax_selected": False}})
    with pytest.raises(ValidationError, match="dedicated draft selection"):
        InvoiceUpdate.model_validate({"withholding_tax_selected": True})
    with pytest.raises(ValidationError, match="dedicated draft selection"):
        InvoiceLineCreate.model_validate(
            {
                "invoice_id": invoice.id,
                "description": "Service",
                "metadata": {"future_invoice": {"withholding_tax_selected": True}},
            }
        )


def test_unselected_issue_snapshot_fails_closed(db_session, subscriber, monkeypatch):
    invoice = _draft(db_session, subscriber, monkeypatch)
    with pytest.raises(owner.InvoiceWithholdingError) as caught:
        owner.snapshot_invoice_withholding_for_issue(db_session, invoice.id)
    assert caught.value.code.endswith("not_selected")


def test_raw_issue_reloads_selected_choice_and_requires_registered_wht_owner(
    db_session, subscriber, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm.attributes import set_committed_value

    from app.models.billing import InvoiceDueDateBasis
    from app.services.billing.invoices import (
        InvoiceIssuanceInput,
        InvoiceOwnerError,
        Invoices,
    )

    invoice = _draft(db_session, subscriber, monkeypatch)
    invoice.withholding_tax_selected = True
    invoice.withholding_tax_selection_version = 1
    db_session.info[owner.INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY] = (
        owner.InvoiceWithholdingSelectionWritePermit(invoice.id, True, 1)
    )
    db_session.commit()
    db_session.info.pop(owner.INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY, None)
    set_committed_value(
        invoice, "withholding_tax_selected", False
    )  # Stale waiter before shared account fence.
    issued_at = datetime.now(UTC)
    issuance = InvoiceIssuanceInput(
        issued_at,
        issued_at + timedelta(days=7),
        InvoiceDueDateBasis.contract_terms,
        "selection-test",
        "v1",
        "Raw issue bypass test",
    )
    with pytest.raises(InvoiceOwnerError) as exc:
        Invoices.issue_draft_system(
            db_session, str(invoice.id), issuance=issuance, commit=False
        )
    assert exc.value.code == "financial.invoice.withholding_issue_owner_required"
    assert invoice.status == InvoiceStatus.draft
    assert invoice.withholding_tax_selected


def test_native_tax_ineligible_draft_cannot_opt_in(db_session, subscriber, monkeypatch):
    from app.services.billing import starter_issuance

    invoice = _draft(db_session, subscriber, monkeypatch)
    monkeypatch.setattr(
        starter_issuance, "eligibility_reason", lambda db, invoice, **kwargs: None
    )
    choice = owner.query_invoice_withholding_selection(
        db_session, owner.InvoiceWithholdingQuery(invoice.id)
    )
    assert not choice.eligible and not choice.selected
    with pytest.raises(owner.InvoiceWithholdingError) as exc:
        _toggle(db_session, invoice, True, 0)
    assert exc.value.code.startswith("financial.invoice_withholding.native_")
