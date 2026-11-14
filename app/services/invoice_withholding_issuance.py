"""Atomic Sub coordinator over existing native Billing issue/WHT/credit writers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Money
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, StarterInvoiceLink
from app.services.billing._common import lock_account
from app.services.billing.invoices import Invoices, verified_draft_issuance
from app.services.domain_errors import DomainError
from app.services.invoice_withholding import (
    InvoiceWithholdingQuery,
    InvoiceWithholdingSnapshot,
    snapshot_invoice_withholding_for_issue,
    stage_invoice_withholding,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class InvoiceWithholdingIssuanceError(DomainError, ValueError):
    pass


def _error(reason: str, message: str) -> InvoiceWithholdingIssuanceError:
    return InvoiceWithholdingIssuanceError(
        code=f"financial.invoice_withholding_issuance.{reason}",
        message=message,
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingFunding:
    billing_account_id: UUID
    source_version: int
    available_credit: Money
    cash_required: Money
    fingerprint: str

    @property
    def reserved_amount(self) -> Decimal:
        return self.cash_required.amount


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingIssuePreview:
    snapshot: InvoiceWithholdingSnapshot
    funding: InvoiceWithholdingFunding
    fingerprint: str


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingIssueConfirm:
    invoice_id: UUID
    preview_fingerprint: str
    evidence_reference: str


def preview_invoice_withholding_issue(
    db: Session, query: InvoiceWithholdingQuery
) -> InvoiceWithholdingIssuePreview:
    from dotmac_billing.contracts import GetCurrentReceivablePositionV1
    from dotmac_billing.queries import get_current_receivable_position

    from app.services.billing.starter_issuance import (
        AdministrativeEligibilityPolicy,
        eligibility_reason,
    )

    invoice = db.get(Invoice, query.invoice_id)
    if (
        invoice is None
        or not invoice.is_active
        or invoice.status != InvoiceStatus.draft
    ):
        raise _error(
            "not_draft", "Only an active draft can be issued with withholding."
        )
    Invoices.validate_service_period_reservations(db, invoice)
    if not invoice.withholding_tax_selected:
        raise _error(
            "not_selected",
            "Select withholding on this draft before issuing it with withholding.",
        )
    reason = eligibility_reason(
        db,
        invoice,
        administrative_policy=AdministrativeEligibilityPolicy.FUTURE_CATALOGUE,
    )
    if reason is not None:
        raise _error(
            "native_issuance_required",
            f"Withholding requires native Billing issuance: {reason}",
        )
    snapshot = snapshot_invoice_withholding_for_issue(db, invoice.id)
    issuance = verified_draft_issuance(
        invoice, issued_at=datetime.now(UTC), reason="issue_with_native_withholding"
    )
    currency = snapshot.tax_fact.base_amount.currency
    current = get_current_receivable_position(
        db,
        scope=_SCOPE,
        request=GetCurrentReceivablePositionV1(
            _SCOPE, snapshot.billing_account_id, currency
        ),
    )
    position = current.position
    cash = Money(snapshot.net_amount, currency)
    if (
        position is None
        or position.completeness != "complete"
        or position.available_credit.amount < cash.amount
    ):
        raise _error(
            "native_cash_shortfall",
            "Native Billing has insufficient verified cash-backed credit. Legacy cash requires verified deposit or opening adoption; withholding cannot create cash.",
        )
    funding = InvoiceWithholdingFunding(
        snapshot.billing_account_id,
        current.source_version,
        position.available_credit,
        cash,
        position.state_fingerprint,
    )
    fingerprint = hashlib.sha256(
        repr(
            (
                snapshot,
                invoice.withholding_tax_selection_version,
                funding,
                issuance.due_at,
                issuance.due_date_basis,
                issuance.due_date_basis_ref,
                issuance.due_date_policy_version,
            )
        ).encode()
    ).hexdigest()
    return InvoiceWithholdingIssuePreview(snapshot, funding, fingerprint)


_ISSUE = OwnerCommandDefinition(
    owner="financial.invoice_withholding_issuance",
    concern="invoice withholding issuance",
    name="confirm_invoice_withholding_issue",
)


def confirm_invoice_withholding_issue(
    db: Session, command: InvoiceWithholdingIssueConfirm, *, context: CommandContext
) -> UUID:
    def operation() -> UUID:
        from dotmac_billing.commands import ApplyAvailableCreditCommand
        from dotmac_billing.contracts import GetCurrentInvoiceReceivableV1
        from dotmac_billing.queries import (
            get_current_invoice_receivable_v1,
            get_withholding_discharge,
        )
        from dotmac_billing.service import apply_available_credit

        from app.services.billing.starter_issuance import (
            AdministrativeEligibilityPolicy,
            issue_admin_invoice_via_starter,
        )

        if (
            context.scope != "billing:invoice:update"
            or not context.actor.startswith("user:")
            or not context.actor.removeprefix("user:").strip()
        ):
            raise _error(
                "invalid_authority",
                "Native withholding issue requires an authorized invoice-update user decision.",
            )
        evidence = command.evidence_reference.strip()
        if not evidence or len(evidence) > 160:
            raise _error(
                "invalid_evidence_reference",
                "Provide the actual withholding declaration reference (at most 160 characters).",
            )
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None:
            raise _error("invoice_not_found", "Invoice not found.")
        lock_account(db, str(invoice.account_id))
        db.refresh(invoice, with_for_update=True)
        link = db.scalar(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice.id
            )
        )
        if invoice.status != InvoiceStatus.draft:
            if (
                link is not None
                and invoice.starter_withholding_discharge_id is not None
            ):
                discharge = get_withholding_discharge(
                    db,
                    scope=_SCOPE,
                    discharge_id=invoice.starter_withholding_discharge_id,
                )
                current = get_current_invoice_receivable_v1(
                    db,
                    scope=_SCOPE,
                    request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
                )
                if (
                    discharge.document_id == link.document_id
                    and discharge.claim_ref == evidence
                    and current.active
                    and current.outstanding.amount == 0
                ):
                    return invoice.id
            raise _error(
                "not_draft",
                "Invoice was already issued from different withholding evidence.",
            )
        Invoices.assert_service_period_reservations_for_issue(db, invoice)
        preview = preview_invoice_withholding_issue(
            db, InvoiceWithholdingQuery(invoice.id)
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise _error(
                "stale_preview",
                "Invoice, native cash position or Tax policy changed; review again.",
            )
        issue_admin_invoice_via_starter(
            db,
            invoice=invoice,
            actor_ref=context.actor,
            administrative_policy=AdministrativeEligibilityPolicy.FUTURE_CATALOGUE,
        )
        link = db.scalar(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice.id
            )
        )
        if link is None:
            raise _error(
                "native_invoice_unlinked",
                "Native issuance produced no authoritative invoice link.",
            )
        stage_invoice_withholding(
            db, snapshot=preview.snapshot, context=context, evidence_reference=evidence
        )
        apply_available_credit(
            db,
            scope=_SCOPE,
            command=ApplyAvailableCreditCommand(
                billing_account_id=link.billing_account_id,
                document_id=link.document_id,
                amount=preview.funding.cash_required,
                occurred_at=datetime.now(UTC),
                source_ref=f"sub-invoice:{invoice.id}:native-credit-withholding",
            ),
        )
        current = get_current_invoice_receivable_v1(
            db,
            scope=_SCOPE,
            request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
        )
        if not current.active or current.outstanding.amount != 0:
            raise _error(
                "native_settlement_incomplete",
                "Native Billing did not fully settle the reviewed invoice.",
            )
        from app.services.billing.starter_settlement import (
            StarterInvoiceProjectionQuery,
            project_starter_invoice_receivable,
        )

        project_starter_invoice_receivable(
            db,
            query=StarterInvoiceProjectionQuery(
                invoice.id, link.document_id, datetime.now(UTC)
            ),
        )
        from app.services.service_entitlements import (
            ensure_prepaid_entitlements_for_paid_invoice,
        )

        ensure_prepaid_entitlements_for_paid_invoice(db, invoice)
        db.flush()
        return invoice.id

    return execute_owner_command(
        db, definition=_ISSUE, context=context, operation=operation
    )
