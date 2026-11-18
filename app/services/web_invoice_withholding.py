"""Read-only admin projections for reviewed invoice withholding corrections."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.schemas.status_presentation import StatusPresentation
from app.services import display_format
from app.services import invoice_withholding as owner
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
    ActionOption,
)
from app.services.invoice_withholding_issuance import InvoiceWithholdingIssuePreview
from app.services.status_presentation import withholding_tax_status_presentation


@dataclass(frozen=True, slots=True)
class WithholdingCorrectionQuery:
    invoice_id: UUID
    payment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WithholdingCorrectionState:
    summary: owner.InvoiceWithholdingSummary
    choices: tuple[owner.InvoiceWithholdingCorrectionChoice, ...]
    preview: owner.InvoiceWithholdingPreview | None
    selected_payment_id: UUID | None


def correction_state(
    db: Session, query: WithholdingCorrectionQuery
) -> WithholdingCorrectionState:
    summary = owner.query_invoice_withholding(
        db, owner.InvoiceWithholdingQuery(query.invoice_id)
    )
    choices = owner.correction_choices(
        db, owner.InvoiceWithholdingQuery(query.invoice_id)
    )
    preview = None
    if query.payment_id is not None:
        preview = owner.preview_invoice_withholding(
            db,
            owner.InvoiceWithholdingPreviewRequest(query.invoice_id, query.payment_id),
        )
    return WithholdingCorrectionState(summary, choices, preview, query.payment_id)


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingIdentity:
    invoice_label: str
    account_label: str


def invoice_identity(
    db: Session, query: owner.InvoiceWithholdingQuery
) -> InvoiceWithholdingIdentity:
    from app.models.billing import Invoice
    from app.models.subscriber import Subscriber
    from app.services.web_billing_payment_forms import account_label

    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        return InvoiceWithholdingIdentity("Selected invoice", "Selected customer")
    account = db.get(Subscriber, invoice.account_id)
    return InvoiceWithholdingIdentity(
        f"Invoice {invoice.invoice_number}"
        if invoice.invoice_number
        else "Draft invoice",
        account_label(account),
    )


def issue_action_form(
    db: Session,
    *,
    invoice_id: UUID,
    preview: InvoiceWithholdingIssuePreview,
    idempotency_token: str,
    error: str | None = None,
) -> ActionForm:
    snapshot = preview.snapshot
    return ActionForm(
        key="invoice.withholding_issue",
        title="Confirm invoice issue",
        description="Use the existing cash deposit and record withholding separately.",
        action_url=f"/admin/billing/invoices/{invoice_id}/issue",
        submit_label="Issue invoice",
        fields=(
            ActionField(
                key="evidence_reference",
                label="WHT claim / supporting evidence reference",
                kind=ActionFieldKind.text,
                required=True,
                max_length=160,
                help_text="Identify the customer's declaration. This does not mark a tax certificate as received.",
            ),
        ),
        hidden_values=(
            ActionHiddenValue("preview_fingerprint", preview.fingerprint),
            ActionHiddenValue("idempotency_token", idempotency_token),
        ),
        impact=f"Invoice total {display_format.format_money(snapshot.gross_amount, db=db, currency=snapshot.currency)}; cash credit {display_format.format_money(snapshot.net_amount, db=db, currency=snapshot.currency)}; withholding {display_format.format_money(snapshot.wht_amount, db=db, currency=snapshot.currency)}. Issue, cash allocation and withholding acceptance happen together. No new bank payment is created.",
        confirmation=ActionConfirmation(
            "I reviewed the invoice and withholding evidence",
            "Use the stated cash credit, accept the separate withholding and issue this invoice. The certificate remains pending.",
        ),
        general_error=error,
    )


def correction_action_form(
    db: Session,
    *,
    query: WithholdingCorrectionQuery,
    state: WithholdingCorrectionState,
    idempotency_token: str,
    evidence_reference: str = "",
    error: str | None = None,
) -> ActionForm:
    if state.preview is None:
        return ActionForm(
            key="invoice.withholding_review",
            title="Review existing cash evidence",
            description="Choose an existing receipt. This review records no new payment.",
            action_url=f"/admin/billing/invoices/{query.invoice_id}/withholding/preview",
            submit_label="Review WHT correction",
            allowed=bool(state.choices),
            disabled_reason=None
            if state.choices
            else state.summary.ineligibility_reason
            or "No eligible exact cash receipt is available.",
            fields=(
                ActionField(
                    key="payment_id",
                    label="Existing net bank receipt",
                    kind=ActionFieldKind.select,
                    required=True,
                    options=tuple(
                        ActionOption(
                            str(choice.payment_id),
                            f"{choice.reference or 'Recorded receipt'} · {display_format.format_money(choice.amount, db=db, currency=state.summary.currency)}",
                        )
                        for choice in state.choices
                    )
                    or (ActionOption("unavailable", "No eligible receipt"),),
                ),
                ActionField(
                    key="evidence_reference",
                    label="WHT claim / supporting evidence reference",
                    kind=ActionFieldKind.text,
                    value=evidence_reference,
                    required=True,
                    max_length=160,
                ),
            ),
            hidden_values=(ActionHiddenValue("idempotency_token", idempotency_token),),
            general_error=error,
        )
    snapshot = state.preview.snapshot
    return ActionForm(
        key="invoice.withholding_correct",
        title="Accept existing withholding evidence",
        description="Record withholding against the cash already allocated to this invoice.",
        action_url=f"/admin/billing/invoices/{query.invoice_id}/withholding/confirm",
        submit_label="Confirm WHT acceptance",
        fields=(),
        hidden_values=(
            ActionHiddenValue("payment_id", str(state.selected_payment_id)),
            ActionHiddenValue("preview_fingerprint", state.preview.fingerprint),
            ActionHiddenValue("evidence_reference", evidence_reference),
            ActionHiddenValue("idempotency_token", idempotency_token),
        ),
        impact=f"Cash already recorded: {display_format.format_money(snapshot.net_amount, db=db, currency=snapshot.currency)}. Accept separate withholding: {display_format.format_money(snapshot.wht_amount, db=db, currency=snapshot.currency)}. No second cash payment or spendable credit is created.",
        confirmation=ActionConfirmation(
            "I reviewed the existing receipt and withholding claim",
            "Accept the separate withholding for this invoice. The certificate remains pending.",
        ),
        general_error=error,
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingDisplay:
    expected_net_amount: str
    expected_wht_amount: str
    accepted_cash_amount: str
    accepted_wht_amount: str
    certificate_outstanding_amount: str
    status: StatusPresentation


def withholding_display(
    db: Session, *, summary: owner.InvoiceWithholdingSummary
) -> InvoiceWithholdingDisplay:
    return InvoiceWithholdingDisplay(
        display_format.format_money(
            summary.expected_net_amount, db=db, currency=summary.currency
        ),
        display_format.format_money(
            summary.expected_wht_amount, db=db, currency=summary.currency
        ),
        display_format.format_money(
            summary.accepted_cash_amount, db=db, currency=summary.currency
        ),
        display_format.format_money(
            summary.accepted_wht_amount, db=db, currency=summary.currency
        ),
        display_format.format_money(
            summary.certificate_outstanding_amount, db=db, currency=summary.currency
        ),
        withholding_tax_status_presentation(summary.status),
    )


@dataclass(frozen=True, slots=True)
class InvoiceTaxDisplay:
    subtotal: str
    discount: str
    vat: str
    total: str
    discount_value: str
    discount_applied_at: str


def invoice_tax_display(db: Session, *, invoice: Invoice) -> InvoiceTaxDisplay:
    value = invoice.discount_value
    discount_value = (
        f"{value.normalize()}%"
        if invoice.discount_type == "percentage" and value is not None
        else display_format.format_money(value, db=db, currency=invoice.currency)
    )
    return InvoiceTaxDisplay(
        display_format.format_money(invoice.subtotal, db=db, currency=invoice.currency),
        display_format.format_money(
            invoice.discount_amount, db=db, currency=invoice.currency
        ),
        display_format.format_money(
            invoice.tax_total, db=db, currency=invoice.currency
        ),
        display_format.format_money(invoice.total, db=db, currency=invoice.currency),
        discount_value,
        display_format.format_timestamp(invoice.discount_applied_at, db),
    )
