"""Atomic application owner for administrative invoice draft authoring."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.services.future_invoice_authoring import FutureInvoiceLineSelection

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import BillingMode, Subscription
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate, InvoiceUpdate
from app.services import invoice_discounts
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _validate_invoice_line_amount,
    lock_account,
)
from app.services.billing.invoices import (
    DraftInvoiceLineReplacement,
    DraftInvoiceNumberingMode,
    DraftInvoiceParticipantError,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
    ProformaConversionInput,
    next_invoice_number,
    verified_draft_issuance,
)
from app.services.common import round_money
from app.services.customer_tax_policies import get_customer_vat_exemption_policy
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.form_contracts import FormConsequence, FormContract
from app.services.form_contracts import register as register_form_contract
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

FUTURE_CATALOGUE_FORM = register_form_contract(
    FormContract(
        key="billing.future_catalogue_invoice",
        title="Reserve future catalogue invoice charges",
        entity="invoice",
        command_owner="financial.invoice_draft_authoring",
        consequences=(
            FormConsequence(
                key="reservation",
                label="Saving the draft reserves selected service periods or one pending capped renewal against duplicate invoices.",
            ),
            FormConsequence(
                key="no_activation",
                label="Saving or issuing an invoice does not activate service; funded entitlement remains settlement-owned.",
            ),
            FormConsequence(
                key="revalidation",
                label="The server checks catalogue prices, VAT, exact service dates and availability again before saving.",
            ),
        ),
    )
)

OWNER = "financial.invoice_draft_authoring"
CONCERN = "administrative invoice draft authoring coordination"
CONVERSION_CONCERN = "administrative proforma conversion coordination"
_CREATE_SCOPE = "invoice-draft-authoring:create"
_CONVERT_SCOPE = "invoice-proforma-conversion"
PROFORMA_TAG = "[PROFORMA]"
PROFORMA_PREFIX = "PF-"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="create_invoice_draft",
)
_UPDATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="update_invoice_draft",
)
_CONVERT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONVERSION_CONCERN,
    name="convert_proforma_invoice",
)


class InvoiceDraftAuthoringError(DomainError, ValueError):
    """Stable rejection from the invoice draft authoring owner."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        DomainError.__init__(self, code=code, message=message, details=details)


@dataclass(frozen=True, slots=True)
class AccountVatSelection:
    account_id: UUID
    tax_rate_id: UUID | None
    tax_rate_name: str | None
    tax_rate_percent: Decimal | None
    selection_locked: bool
    vat_exempt: bool


def _error(suffix: str, message: str, **details: object) -> InvoiceDraftAuthoringError:
    return InvoiceDraftAuthoringError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class IssueAdministrativeInvoiceCommand:
    invoice_id: UUID
    announce: bool = False


def issue_administrative_invoice(
    db: Session, command: IssueAdministrativeInvoiceCommand, *, context: CommandContext
) -> UUID:
    """Issue a reviewed administrative draft through incumbent financial writers."""
    definition = OwnerCommandDefinition(
        owner=OWNER,
        concern="administrative invoice issue coordination",
        name="issue_administrative_invoice",
    )

    def operation() -> UUID:
        from app.models.billing import StarterInvoiceLink
        from app.services.billing import starter_issuance
        from app.services.billing.starter_settlement import (
            StarterInvoiceProjectionQuery,
            apply_native_account_credit_for_invoice,
        )
        from app.services.manual_payment_recording import (
            NativeAccountCashQuery,
            uses_native_account_cash,
        )
        from app.services.prepaid_draft_reconciliation import (
            preview_prepaid_draft_reconciliation,
        )

        if context.scope != "billing:invoice:update":
            raise _error(
                "invalid_issue_authority",
                "Invoice issue requires invoice-update authority.",
            )
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None or not invoice.is_active:
            raise _error("invoice_not_found", "Invoice not found.")
        lock_account(db, str(invoice.account_id))
        db.refresh(invoice, with_for_update=True)
        if invoice.withholding_tax_selected:
            raise _error(
                "withholding_review_required",
                "Review the selected withholding settlement before issuing this invoice.",
            )
        if invoice.status != InvoiceStatus.draft:
            if invoice.issued_at is not None and invoice.status not in {
                InvoiceStatus.void,
                InvoiceStatus.written_off,
            }:
                # Exact document issuance provenance makes a repeated Issue a no-op.
                return invoice.id
            raise _error("issue_rejected", "Only draft invoices can be issued.")
        Invoices.assert_service_period_reservations_for_issue(db, invoice)
        try:
            reconciliation = preview_prepaid_draft_reconciliation(db, invoice.id)
        except DomainError:
            reconciliation = None
        if (
            reconciliation is not None
            and reconciliation.invoice_issue_notice is not None
        ):
            raise _error(
                "prepaid_coverage_conflict", reconciliation.invoice_issue_notice
            )
        native_cash = uses_native_account_cash(
            db, NativeAccountCashQuery(invoice.account_id)
        )
        native_reason = starter_issuance.eligibility_reason(
            db,
            invoice,
            administrative_policy=starter_issuance.AdministrativeEligibilityPolicy.FUTURE_CATALOGUE,
        )
        if native_reason is None:
            starter_issuance.issue_admin_invoice_via_starter(
                db,
                invoice=invoice,
                actor_ref=context.actor,
                announce=command.announce,
                administrative_policy=starter_issuance.AdministrativeEligibilityPolicy.FUTURE_CATALOGUE,
            )
            if native_cash:
                link = db.scalar(
                    select(StarterInvoiceLink).where(
                        StarterInvoiceLink.invoice_id == invoice.id
                    )
                )
                if link is None:
                    raise _error(
                        "native_invoice_unlinked",
                        "Native issuance returned no structural invoice link.",
                    )
                apply_native_account_credit_for_invoice(
                    db,
                    query=StarterInvoiceProjectionQuery(
                        invoice.id, link.document_id, datetime.now(UTC)
                    ),
                )
        else:
            if native_cash:
                raise _error(
                    "native_issue_required",
                    "This account has native cash; its invoice must use the supported native issuance lane: "
                    + native_reason,
                )
            issued_at = invoice.issued_at or datetime.now(UTC)
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=UTC)
            Invoices.issue_draft_system(
                db,
                str(invoice.id),
                issuance=verified_draft_issuance(
                    invoice, issued_at=issued_at, reason="admin_invoice_detail_issue"
                ),
                announce=command.announce,
                apply_available_credit=True,
                require_full_available_credit=True,
                commit=False,
            )
        db.flush()
        return invoice.id

    return execute_owner_command(
        db, definition=definition, context=context, operation=operation
    )


def resolve_account_vat_selection(
    db: Session, *, account: Subscriber
) -> AccountVatSelection:
    """Resolve account VAT once for both the draft command and its form."""

    policy = get_customer_vat_exemption_policy(db, account_id=account.id)
    rate = db.get(TaxRate, account.tax_rate_id) if account.tax_rate_id else None
    active_rate = None if policy.vat_exempt or not rate or not rate.is_active else rate
    return AccountVatSelection(
        account_id=account.id,
        tax_rate_id=active_rate.id if active_rate else None,
        tax_rate_name=active_rate.name if active_rate else None,
        tax_rate_percent=Decimal(str(active_rate.rate)) if active_rate else None,
        selection_locked=active_rate is not None,
        vat_exempt=policy.vat_exempt,
    )


def apply_proforma_form_values(
    *,
    invoice_number: str | None,
    memo: str | None,
    proforma_invoice: bool,
) -> tuple[str | None, str | None]:
    """Normalize the documentary marker for draft authoring and conversion."""

    clean_number = (invoice_number or "").strip() or None
    clean_memo = (memo or "").strip() or None
    if proforma_invoice:
        if clean_number and not clean_number.upper().startswith(PROFORMA_PREFIX):
            clean_number = f"{PROFORMA_PREFIX}{clean_number}"
        if clean_memo:
            if PROFORMA_TAG not in clean_memo:
                clean_memo = f"{PROFORMA_TAG} {clean_memo}".strip()
        else:
            clean_memo = PROFORMA_TAG
        return clean_number, clean_memo
    if clean_number and clean_number.upper().startswith(PROFORMA_PREFIX):
        clean_number = clean_number[len(PROFORMA_PREFIX) :].strip() or None
    if clean_memo and PROFORMA_TAG in clean_memo:
        clean_memo = clean_memo.replace(PROFORMA_TAG, "").strip() or None
    return clean_number, clean_memo


def _is_active_proforma(invoice: Invoice) -> bool:
    """Recognize the canonical flag and supported historical documentary markers."""

    number = (invoice.invoice_number or "").strip().upper()
    memo = invoice.memo or ""
    return bool(
        invoice.is_proforma
        or number.startswith(PROFORMA_PREFIX)
        or PROFORMA_TAG in memo
    )


def _requires_prepaid_reconciliation(db: Session, invoice: Invoice) -> bool:
    """Fail closed when generic conversion would bypass prepaid settlement."""

    account_billing_mode = db.scalar(
        select(Subscriber.billing_mode).where(Subscriber.id == invoice.account_id)
    )
    if account_billing_mode is BillingMode.prepaid:
        return True
    prepaid_subscription_id = db.scalar(
        select(Subscription.id)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.is_active.is_(True),
            Subscription.billing_mode == BillingMode.prepaid,
        )
        .limit(1)
    )
    return prepaid_subscription_id is not None


@dataclass(frozen=True, slots=True)
class DraftLineCommand:
    """One complete line in the desired draft document."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate_id: UUID | None = None
    subscription_id: UUID | None = None
    line_id: UUID | None = None
    catalogue_selection: FutureInvoiceLineSelection | None = None
    resolved_tax_application: TaxApplication | None = None
    resolved_metadata: dict[str, object] | str | None = None


@dataclass(frozen=True, slots=True)
class CreateInvoiceDraftCommand:
    """Complete desired state for a new administrative invoice draft."""

    account_id: UUID
    invoice_number: str | None
    currency: str
    issued_at: datetime | None
    due_at: datetime | None
    memo: str | None
    is_proforma: bool
    lines: tuple[DraftLineCommand, ...]
    discount: invoice_discounts.InvoiceDiscountInput | None = None
    actor_system_user_id: UUID | None = None
    require_catalogue: bool = False


@dataclass(frozen=True, slots=True)
class UpdateInvoiceDraftCommand:
    """Complete desired state for an existing administrative invoice draft."""

    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    currency: str
    issued_at: datetime | None
    due_at: datetime | None
    memo: str | None
    is_proforma: bool
    lines: tuple[DraftLineCommand, ...]
    discount: invoice_discounts.InvoiceDiscountInput | None = None
    actor_system_user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConvertProformaInvoiceCommand:
    """Convert one exact proforma identity using owner-derived current state."""

    invoice_id: UUID


@dataclass(frozen=True, slots=True)
class ProformaConversionCapability:
    """Owner-derived eligibility for the generic proforma conversion action."""

    allowed: bool
    reason: str | None = None


def proforma_conversion_capability(
    db: Session, *, invoice: Invoice
) -> ProformaConversionCapability:
    """Return whether generic conversion can safely be offered for an invoice."""

    if not invoice.is_active:
        return ProformaConversionCapability(
            allowed=False,
            reason="Inactive invoices cannot be converted.",
        )
    if not _is_active_proforma(invoice):
        return ProformaConversionCapability(
            allowed=False,
            reason="Invoice is not an active proforma.",
        )
    if invoice.status is not InvoiceStatus.draft:
        return ProformaConversionCapability(
            allowed=False,
            reason="Only a draft proforma can be converted.",
        )
    if _requires_prepaid_reconciliation(db, invoice):
        return ProformaConversionCapability(
            allowed=False,
            reason=(
                "Prepaid proformas are handled through the reviewed prepaid draft "
                "reconciliation workflow after verified funding."
            ),
        )
    return ProformaConversionCapability(allowed=True)


@dataclass(frozen=True, slots=True)
class InvoiceDraftResult:
    """Immutable result returned after the owner transaction commits."""

    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    status: InvoiceStatus
    total: Decimal
    balance_due: Decimal
    is_proforma: bool
    replayed: bool = False


def _result(invoice: Invoice, *, replayed: bool = False) -> InvoiceDraftResult:
    return InvoiceDraftResult(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        status=invoice.status,
        total=round_money(invoice.total),
        balance_due=round_money(invoice.balance_due),
        is_proforma=bool(invoice.is_proforma),
        replayed=replayed,
    )


def _normalized_key(context: CommandContext) -> str:
    source = context.idempotency_key or str(context.command_id)
    return hashlib.sha256(source.strip().encode()).hexdigest()


def _create_fingerprint(command: CreateInvoiceDraftCommand) -> str:
    payload = {
        "account_id": str(command.account_id),
        **({"require_catalogue": True} if command.require_catalogue else {}),
        "actor_system_user_id": (
            str(command.actor_system_user_id) if command.actor_system_user_id else None
        ),
        "invoice_number": (command.invoice_number or "").strip() or None,
        "currency": command.currency.strip().upper(),
        "issued_at": command.issued_at.isoformat() if command.issued_at else None,
        "due_at": command.due_at.isoformat() if command.due_at else None,
        "memo": command.memo,
        "is_proforma": command.is_proforma,
        "lines": [
            {
                **(
                    {"catalogue_selection": _catalogue_selection_wire(line)}
                    if line.catalogue_selection is not None
                    else {}
                ),
                "description": line.description.strip(),
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
                "subscription_id": (
                    str(line.subscription_id) if line.subscription_id else None
                ),
            }
            for line in command.lines
        ],
        "discount": (
            {
                "type": command.discount.discount_type.value,
                "value": str(command.discount.value),
                "reason": command.discount.reason,
            }
            if command.discount
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _conversion_fingerprint(command: ConvertProformaInvoiceCommand) -> str:
    return hashlib.sha256(
        json.dumps(
            {"invoice_id": str(command.invoice_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validate_account(db: Session, account_id: UUID) -> None:
    lock_account(db, str(account_id))
    if db.get(Subscriber, account_id) is None:
        raise _error(
            "account_not_found",
            "Subscriber account was not found.",
            account_id=str(account_id),
        )


def _validate_header(
    db: Session,
    *,
    account_id: UUID,
    invoice_number: str | None,
    currency: str,
    exclude_invoice_id: UUID | None = None,
) -> tuple[str | None, str]:
    _validate_account(db, account_id)
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise _error("currency_invalid", "Invoice currency must be a 3-letter code.")
    normalized_number = (invoice_number or "").strip() or None
    if normalized_number:
        duplicate = (
            select(Invoice.id)
            .where(Invoice.invoice_number == normalized_number)
            .where(Invoice.is_active.is_(True))
        )
        if exclude_invoice_id is not None:
            duplicate = duplicate.where(Invoice.id != exclude_invoice_id)
        if db.scalar(duplicate) is not None:
            raise _error(
                "invoice_number_conflict",
                "Invoice number is already in use.",
                invoice_number=normalized_number,
            )
    return normalized_number, normalized_currency


def _catalogue_selection_wire(line: DraftLineCommand) -> dict[str, object] | None:
    if line.catalogue_selection is None:
        return None
    from app.services.future_invoice_authoring import selection_to_wire

    return selection_to_wire(line.catalogue_selection)


def _resolve_catalogue_line(
    db: Session,
    *,
    line: DraftLineCommand,
    account_id: UUID,
    currency: str,
    invoice_id: UUID | None,
) -> DraftLineCommand:
    from app.services.future_invoice_authoring import (
        confirm_future_invoice_line,
        preview_line_metadata,
    )

    assert line.catalogue_selection is not None
    preview = confirm_future_invoice_line(
        db,
        account_id=account_id,
        currency=currency,
        selection=line.catalogue_selection,
        invoice_id=invoice_id,
    )
    return DraftLineCommand(
        description=preview.description,
        quantity=preview.quantity,
        unit_price=preview.unit_price,
        tax_rate_id=preview.tax_rate_id,
        subscription_id=preview.subscription_id,
        line_id=line.line_id,
        catalogue_selection=line.catalogue_selection,
        resolved_tax_application=preview.tax_application,
        resolved_metadata=preview_line_metadata(preview),
    )


def _validated_lines(
    db: Session,
    *,
    account_id: UUID,
    lines: tuple[DraftLineCommand, ...],
    currency: str = "NGN",
    invoice_id: UUID | None = None,
    require_catalogue: bool = False,
) -> tuple[tuple[DraftLineCommand, Decimal, TaxApplication], ...]:
    if not lines:
        raise _error(
            "line_required",
            "A draft invoice must contain at least one line item.",
        )
    validated: list[tuple[DraftLineCommand, Decimal, TaxApplication]] = []
    for line in lines:
        if line.catalogue_selection is not None:
            line = _resolve_catalogue_line(
                db,
                line=line,
                account_id=account_id,
                currency=currency,
                invoice_id=invoice_id,
            )
        elif require_catalogue:
            raise _error(
                "catalogue_selection_required",
                "Every new invoice line must be selected from the catalogue.",
            )
        else:
            previous = db.get(InvoiceLine, line.line_id) if line.line_id else None
            if previous is not None and previous.invoice_id == invoice_id:
                line = replace(
                    line,
                    subscription_id=previous.subscription_id,
                    resolved_tax_application=previous.tax_application,
                    resolved_metadata=previous.metadata_
                    if isinstance(previous.metadata_, (dict, str))
                    else None,
                )
            else:
                line = replace(
                    line, resolved_tax_application=None, resolved_metadata=None
                )
        description = line.description.strip()
        if not description:
            raise _error("line_invalid", "Invoice line description is required.")
        if len(description) > 255:
            raise _error(
                "line_invalid",
                "Invoice line description cannot exceed 255 characters.",
            )
        try:
            amount = _validate_invoice_line_amount(
                line.quantity,
                line.unit_price,
                None,
            )
        except Exception as exc:
            raise _error(
                "line_invalid",
                "Invoice line quantity or price is invalid.",
                description=description,
            ) from exc
        if line.tax_rate_id is not None and db.get(TaxRate, line.tax_rate_id) is None:
            raise _error(
                "tax_rate_not_found",
                "Invoice tax rate was not found.",
                tax_rate_id=str(line.tax_rate_id),
            )
        if line.subscription_id is not None:
            subscription = db.get(Subscription, line.subscription_id)
            if subscription is None:
                raise _error(
                    "subscription_not_found",
                    "Invoice line subscription was not found.",
                    subscription_id=str(line.subscription_id),
                )
            if subscription.subscriber_id != account_id:
                raise _error(
                    "subscription_account_mismatch",
                    "Invoice line subscription does not belong to this account.",
                    subscription_id=str(line.subscription_id),
                    account_id=str(account_id),
                )
        validated.append(
            (
                DraftLineCommand(
                    description=description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    tax_rate_id=line.tax_rate_id,
                    subscription_id=line.subscription_id,
                    line_id=line.line_id,
                    catalogue_selection=line.catalogue_selection,
                    resolved_tax_application=line.resolved_tax_application,
                    resolved_metadata=line.resolved_metadata,
                ),
                amount,
                line.resolved_tax_application or TaxApplication.exclusive,
            )
        )
    return tuple(validated)


def _is_capped_catalogue_line(line: DraftLineCommand) -> bool:
    evidence = (
        line.resolved_metadata.get("future_invoice")
        if isinstance(line.resolved_metadata, dict)
        else None
    )
    return (
        isinstance(evidence, dict)
        and evidence.get("period_kind") == "capped_pending_renewal"
    )


def _stage_lines(
    db: Session,
    *,
    invoice: Invoice,
    lines: tuple[DraftLineCommand, ...],
    require_catalogue: bool = False,
    preview_invoice_id: UUID | None = None,
) -> None:
    validated = _validated_lines(
        db,
        account_id=invoice.account_id,
        lines=lines,
        currency=invoice.currency,
        invoice_id=preview_invoice_id,
        require_catalogue=require_catalogue,
    )
    catalogue_lines = [
        entry[0] for entry in validated if entry[0].catalogue_selection is not None
    ]
    capped_lines = [line for line in catalogue_lines if _is_capped_catalogue_line(line)]
    if capped_lines and len(catalogue_lines) != 1:
        raise _error(
            "capped_renewal_single_line",
            "A pending quota renewal must be the only invoice line.",
        )
    account = db.get(Subscriber, invoice.account_id)
    if account is None:
        raise _error(
            "account_not_found",
            "Customer account was not found.",
            account_id=str(invoice.account_id),
        )
    account_vat = resolve_account_vat_selection(db, account=account)
    replacements = tuple(
        DraftInvoiceLineReplacement(
            line_id=command.line_id,
            payload=InvoiceLineCreate(
                invoice_id=invoice.id,
                description=command.description,
                quantity=command.quantity,
                unit_price=command.unit_price,
                amount=amount,
                tax_rate_id=(
                    None
                    if account_vat.vat_exempt
                    else command.tax_rate_id
                    if command.catalogue_selection is not None
                    else account_vat.tax_rate_id or command.tax_rate_id
                ),
                tax_application=tax_application,
                subscription_id=command.subscription_id,
                metadata_=command.resolved_metadata,
                is_active=True,
            ),
        )
        for command, amount, tax_application in validated
    )
    try:
        InvoiceLines.replace_admin_draft_lines(
            db,
            invoice.id,
            replacements,
            allow_discount_reprice=True,
        )
    except DraftInvoiceParticipantError as exc:
        if exc.reason == "line_not_found":
            raise _error(
                "line_not_found",
                "Invoice line does not belong to this draft.",
            ) from exc
        raise _error(exc.reason, str(exc)) from exc


def _stage_audit(
    db: Session,
    *,
    invoice: Invoice,
    context: CommandContext,
    action: str,
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_id=context.actor,
            action=action,
            entity_type="invoice",
            entity_id=str(invoice.id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(invoice.account_id),
                "invoice_number": invoice.invoice_number,
                "status": invoice.status.value,
                "is_proforma": bool(invoice.is_proforma),
                "total": str(invoice.total),
                "command_id": str(context.command_id),
                "reason": context.reason,
                "financial_effect": "draft_document_authored",
            },
        ),
    )


def _emit_created(db: Session, invoice: Invoice) -> None:
    emit_event(
        db,
        EventType.invoice_created,
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "amount": str(invoice.total),
            "total": str(invoice.total),
            "due_date": invoice.due_at.date().isoformat() if invoice.due_at else None,
            "currency": invoice.currency,
            "status": invoice.status.value,
            "is_proforma": bool(invoice.is_proforma),
        },
        account_id=invoice.account_id,
        invoice_id=invoice.id,
    )


def create_invoice_draft(
    db: Session,
    command: CreateInvoiceDraftCommand,
    *,
    context: CommandContext,
) -> InvoiceDraftResult:
    """Create one complete draft, its lines, audit, and event atomically."""

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=lambda: _create_invoice_draft(db, command=command, context=context),
    )


def _create_invoice_draft(
    db: Session,
    *,
    command: CreateInvoiceDraftCommand,
    context: CommandContext,
) -> InvoiceDraftResult:
    key = _normalized_key(context)
    fingerprint = _create_fingerprint(command)
    _validate_account(db, command.account_id)
    replay = db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == _CREATE_SCOPE)
        .where(IdempotencyKey.key == key)
        .with_for_update()
    )
    if replay is not None:
        if replay.account_id != command.account_id or not replay.ref_id:
            raise _error(
                "idempotency_conflict",
                "Draft creation key is already reserved for another result.",
            )
        result_parts = replay.ref_id.split("|", maxsplit=1)
        if len(result_parts) != 2 or result_parts[1] != fingerprint:
            raise _error(
                "idempotency_conflict",
                "Draft creation key was reused with different invoice data.",
            )
        try:
            replay_invoice_id = UUID(result_parts[0])
        except ValueError as exc:
            raise _error(
                "idempotency_conflict",
                "Draft creation result evidence is invalid.",
            ) from exc
        invoice = db.get(Invoice, replay_invoice_id)
        if invoice is None:
            raise _error(
                "idempotency_conflict",
                "Draft creation result can no longer be resolved.",
            )
        return _result(invoice, replayed=True)

    invoice_number, currency = _validate_header(
        db,
        account_id=command.account_id,
        invoice_number=command.invoice_number,
        currency=command.currency,
    )
    reservation = IdempotencyKey(
        scope=_CREATE_SCOPE,
        key=key,
        account_id=command.account_id,
    )
    db.add(reservation)
    db.flush()

    invoice = Invoices.stage_admin_draft(
        db,
        InvoiceCreate(
            account_id=command.account_id,
            invoice_number=invoice_number,
            status=InvoiceStatus.draft,
            currency=currency,
            issued_at=command.issued_at,
            due_at=command.due_at,
            due_date_basis=(
                InvoiceDueDateBasis.approved_manual_override
                if command.due_at is not None
                else None
            ),
            due_date_basis_ref=(
                f"invoice-draft-command:{context.command_id}"
                if command.due_at is not None
                else None
            ),
            due_date_policy_version=(
                "admin-invoice-draft-v1" if command.due_at is not None else None
            ),
            memo=command.memo,
            is_proforma=command.is_proforma,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            is_active=True,
        ),
        numbering_mode=(
            DraftInvoiceNumberingMode.AT_ISSUANCE
            if command.require_catalogue and not command.is_proforma
            else DraftInvoiceNumberingMode.AT_DRAFT
        ),
    )
    _stage_lines(
        db,
        invoice=invoice,
        lines=command.lines,
        require_catalogue=command.require_catalogue,
    )
    if command.discount is not None:
        if command.actor_system_user_id is None:
            raise _error(
                "discount_actor_required",
                "A logged-in staff user is required to apply an Invoice discount.",
            )
        invoice_discounts.stage_invoice_discount(
            db,
            invoice,
            invoice_discounts.StageInvoiceDiscountCommand(
                invoice_id=invoice.id,
                actor_system_user_id=command.actor_system_user_id,
                command_id=context.command_id,
                discount=command.discount,
            ),
        )
    _stage_audit(
        db,
        invoice=invoice,
        context=context,
        action="create_invoice_draft",
    )
    _emit_created(db, invoice)
    reservation.ref_id = f"{invoice.id}|{fingerprint}"
    db.flush()
    return _result(invoice)


def update_invoice_draft(
    db: Session,
    command: UpdateInvoiceDraftCommand,
    *,
    context: CommandContext,
) -> InvoiceDraftResult:
    """Replace one draft's editable header and line state atomically."""

    return execute_owner_command(
        db,
        definition=_UPDATE_COMMAND,
        context=context,
        operation=lambda: _update_invoice_draft(db, command=command, context=context),
    )


def _update_invoice_draft(
    db: Session,
    *,
    command: UpdateInvoiceDraftCommand,
    context: CommandContext,
) -> InvoiceDraftResult:
    invoice_number, currency = _validate_header(
        db,
        account_id=command.account_id,
        invoice_number=command.invoice_number,
        currency=command.currency,
        exclude_invoice_id=command.invoice_id,
    )
    invoice = lock_for_update(db, Invoice, command.invoice_id)
    if invoice is None or not invoice.is_active:
        raise _error(
            "invoice_not_found",
            "Invoice draft was not found.",
            invoice_id=str(command.invoice_id),
        )
    if invoice.account_id != command.account_id:
        raise _error(
            "account_mismatch",
            "Invoice account cannot be changed.",
            invoice_id=str(invoice.id),
        )
    if invoice.status != InvoiceStatus.draft:
        raise _error(
            "invoice_not_editable",
            "Only draft invoices can be edited.",
            invoice_id=str(invoice.id),
            status=invoice.status.value,
        )
    if invoice.currency != currency:
        raise _error(
            "currency_mismatch",
            "Invoice currency cannot be changed after creation.",
            invoice_id=str(invoice.id),
        )

    invoice = Invoices.stage_admin_draft_header(
        db,
        invoice.id,
        InvoiceUpdate(
            invoice_number=invoice_number or invoice.invoice_number,
            issued_at=command.issued_at,
            due_at=command.due_at,
            due_date_basis=(
                InvoiceDueDateBasis.approved_manual_override
                if command.due_at is not None
                else None
            ),
            due_date_basis_ref=(
                f"invoice-draft-command:{context.command_id}"
                if command.due_at is not None
                else None
            ),
            due_date_policy_version=(
                "admin-invoice-draft-v1" if command.due_at is not None else None
            ),
            memo=command.memo,
            is_proforma=command.is_proforma,
        ),
    )
    _stage_lines(
        db,
        invoice=invoice,
        lines=command.lines,
        require_catalogue=any(
            isinstance(line.metadata_, dict)
            and line.metadata_.get("future_catalogue_invoice") is True
            for line in invoice.lines
            if line.is_active
        ),
        preview_invoice_id=invoice.id,
    )
    if command.discount is not None or invoice.discount_type is not None:
        if command.actor_system_user_id is None:
            raise _error(
                "discount_actor_required",
                "A logged-in staff user is required to change an Invoice discount.",
            )
        invoice_discounts.stage_invoice_discount(
            db,
            invoice,
            invoice_discounts.StageInvoiceDiscountCommand(
                invoice_id=invoice.id,
                actor_system_user_id=command.actor_system_user_id,
                command_id=context.command_id,
                discount=command.discount,
            ),
        )
    _stage_audit(
        db,
        invoice=invoice,
        context=context,
        action="update_invoice_draft",
    )
    db.flush()
    return _result(invoice)


def convert_proforma_invoice(
    db: Session,
    command: ConvertProformaInvoiceCommand,
    *,
    context: CommandContext,
) -> InvoiceDraftResult:
    """Convert one proforma under locks and replay retries from durable evidence."""

    return execute_owner_command(
        db,
        definition=_CONVERT_COMMAND,
        context=context,
        operation=lambda: _convert_proforma_invoice(
            db,
            command=command,
            context=context,
        ),
    )


def _convert_proforma_invoice(
    db: Session,
    *,
    command: ConvertProformaInvoiceCommand,
    context: CommandContext,
) -> InvoiceDraftResult:
    account_id = db.scalar(
        select(Invoice.account_id).where(Invoice.id == command.invoice_id)
    )
    if account_id is None:
        raise _error(
            "invoice_not_found",
            "Proforma invoice was not found.",
            invoice_id=str(command.invoice_id),
        )
    _validate_account(db, account_id)
    invoice = lock_for_update(db, Invoice, command.invoice_id)
    if invoice is None or not invoice.is_active:
        raise _error(
            "invoice_not_found",
            "Proforma invoice was not found.",
            invoice_id=str(command.invoice_id),
        )

    key = _normalized_key(context)
    fingerprint = _conversion_fingerprint(command)
    replay = db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == _CONVERT_SCOPE)
        .where(IdempotencyKey.key == key)
        .with_for_update()
    )
    if replay is not None:
        expected_ref = f"{invoice.id}|{fingerprint}"
        if replay.account_id != account_id or replay.ref_id != expected_ref:
            raise _error(
                "idempotency_conflict",
                "Proforma conversion key was used for a different invoice.",
            )
        return _result(invoice, replayed=True)
    if not _is_active_proforma(invoice):
        raise _error(
            "invoice_not_proforma",
            "Invoice is not an active proforma.",
            invoice_id=str(invoice.id),
        )
    if invoice.status != InvoiceStatus.draft:
        raise _error(
            "invoice_not_editable",
            "Only a draft proforma can be converted.",
            invoice_id=str(invoice.id),
            status=invoice.status.value,
        )
    if _requires_prepaid_reconciliation(db, invoice):
        raise _error(
            "prepaid_reconciliation_required",
            (
                "Prepaid proformas cannot use generic conversion; run the reviewed "
                "prepaid proforma adoption and draft reconciliation workflow."
            ),
            invoice_id=str(invoice.id),
            account_id=str(invoice.account_id),
        )

    invoice_number = (invoice.invoice_number or "").strip() or None
    if invoice_number and invoice_number.upper().startswith(PROFORMA_PREFIX):
        invoice_number = next_invoice_number(db)
    _, cleaned_memo = apply_proforma_form_values(
        invoice_number=invoice_number,
        memo=invoice.memo,
        proforma_invoice=False,
    )
    reservation = IdempotencyKey(
        scope=_CONVERT_SCOPE,
        key=key,
        account_id=account_id,
    )
    db.add(reservation)
    db.flush()
    try:
        transition = Invoices.convert_proforma_for_owner(
            db,
            ProformaConversionInput(
                invoice_id=invoice.id,
                invoice_number=invoice_number,
                memo=cleaned_memo,
                issued_at=datetime.now(UTC),
                reason=context.reason,
            ),
        )
    except InvoiceOwnerError as exc:
        raise _error(
            "conversion_rejected",
            exc.message,
            invoice_id=str(invoice.id),
            owner_code=exc.code,
        ) from exc
    converted = transition.invoice
    reservation.ref_id = f"{converted.id}|{fingerprint}"
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_id=context.actor,
            action="convert_proforma_invoice",
            entity_type="invoice",
            entity_id=str(converted.id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(converted.account_id),
                "invoice_number": converted.invoice_number,
                "status": converted.status.value,
                "command_id": str(context.command_id),
                "reason": context.reason,
                "financial_effect": "existing_account_credit_applied_by_owner",
            },
        ),
    )
    db.flush()
    return _result(converted)
