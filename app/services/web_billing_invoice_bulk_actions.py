"""Server-owned bulk interaction projection for the admin invoice list."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.services import web_billing_invoice_bulk as invoice_bulk_service
from app.services.auth_dependencies import has_permission
from app.services.bulk_actions import BulkActionDefinition, BulkResourceDefinition

INVOICE_BULK_ACTION_DEFINITION = BulkResourceDefinition(
    key="billing_invoices",
    actions=(
        BulkActionDefinition(
            key="issue",
            label="Issue",
            description="Issue selected draft invoices.",
            permission="billing:invoice:update",
            tone="info",
        ),
        BulkActionDefinition(
            key="send",
            label="Send",
            description="Queue email delivery for eligible issued invoices.",
            permission="billing:invoice:update",
            tone="positive",
        ),
        BulkActionDefinition(
            key="void",
            label="Void",
            description="Void eligible invoices and reverse their ledger effect.",
            permission="billing:invoice:delete",
            tone="negative",
        ),
        BulkActionDefinition(
            key="mark_paid",
            label="Mark paid",
            description="Record and allocate payment for open invoice balances.",
            permission="billing:invoice:update",
            tone="positive",
        ),
        BulkActionDefinition(
            key="generate_pdf",
            label="Generate PDFs",
            description="Queue PDF generation for selected invoices.",
            permission="billing:invoice:read",
            tone="info",
            execution_mode="queued",
            result_reference="/admin/billing/invoices/bulk/pdf-ready",
        ),
        BulkActionDefinition(
            key="export_csv",
            label="Export CSV",
            description="Download selected invoices as CSV.",
            permission="billing:invoice:read",
            tone="neutral",
            requires_preview=False,
            requires_confirmation=False,
        ),
        BulkActionDefinition(
            key="export_pdf",
            label="Export PDFs",
            description="Download ready invoice PDFs as a ZIP archive.",
            permission="billing:invoice:read",
            tone="neutral",
            requires_preview=False,
            requires_confirmation=False,
        ),
    ),
)


def build_invoice_bulk_action_contract(
    db: Session,
    *,
    auth: dict,
    invoices: Sequence[Invoice],
) -> dict[str, object]:
    """Project authorized actions and page-row eligibility without policy copies."""

    declared_permissions = {
        action.permission for action in INVOICE_BULK_ACTION_DEFINITION.actions
    }
    authorized_permissions = {
        permission
        for permission in declared_permissions
        if auth and has_permission(auth, db, permission)
    }
    contract = INVOICE_BULK_ACTION_DEFINITION.project(
        authorized_permissions=authorized_permissions
    ).as_dict()
    actions = contract["actions"]
    assert isinstance(actions, list)
    for action in actions:
        assert isinstance(action, dict)
        action_key = str(action["key"])
        eligible_ids: list[str] = []
        ineligible_reasons: dict[str, str] = {}
        for invoice in invoices:
            invoice_id = str(invoice.id)
            reason = invoice_bulk_service.invoice_bulk_action_ineligibility(
                invoice, action_key
            )
            if reason:
                ineligible_reasons[invoice_id] = reason
            else:
                eligible_ids.append(invoice_id)
        action["eligible_ids"] = eligible_ids
        action["ineligible_reasons"] = ineligible_reasons
    return contract
