"""Sub facts and projections around existing Tax and Billing WHT authorities.

No local withholding receivable, cash, allocation, or AR engine lives here.
Legacy gross records remain historical evidence. New settlement requires genuine
native Tax policy, native Billing documents, and native cash-backed credit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_tax.contracts import TaxFact
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY,
    Invoice,
    InvoiceStatus,
    InvoiceWithholdingSelectionWritePermit,
    StarterInvoiceLink,
)
from app.models.domain_settings import SettingDomain
from app.models.payment_proof import WithholdingTaxRecord, WithholdingTaxStatus
from app.services import settings_spec
from app.services.billing._common import (
    lock_account,
    resolve_invoice_settlement_amounts,
)
from app.services.common import to_decimal
from app.services.customer_tax_policies import get_customer_withholding_tax_policy
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from dotmac_billing.contracts import BillingAccountObservationV1
    from dotmac_tax.contracts import TaxPolicyPreviewV1

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class InvoiceWithholdingError(DomainError, ValueError):
    pass


def _error(reason: str, message: str) -> InvoiceWithholdingError:
    return InvoiceWithholdingError(
        code=f"financial.invoice_withholding.{reason}", message=message, retryable=False
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingQuery:
    invoice_id: UUID


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingPreviewRequest:
    invoice_id: UUID
    existing_payment_id: UUID | None = None
    existing_payment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingSnapshot:
    invoice_id: UUID
    account_id: UUID
    currency: str
    gross_amount: Decimal
    net_amount: Decimal
    wht_amount: Decimal
    wht_rate: Decimal
    vat_exclusive_amount: Decimal
    vat_amount: Decimal
    policy_version: int
    billing_account_id: UUID
    tax_fact: TaxFact
    tax_preview: TaxPolicyPreviewV1
    tax_code_id: UUID


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingPreview:
    snapshot: InvoiceWithholdingSnapshot
    fingerprint: str
    existing_payment_id: UUID | None


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingConfirm:
    request: InvoiceWithholdingPreviewRequest
    preview_fingerprint: str
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingSummary:
    invoice_id: UUID
    currency: str
    gross_amount: Decimal
    vat_exclusive_amount: Decimal
    vat_amount: Decimal
    expected_net_amount: Decimal | None
    expected_wht_amount: Decimal | None
    accepted_cash_amount: Decimal
    accepted_wht_amount: Decimal
    balance_due: Decimal
    record_id: UUID | None
    status: WithholdingTaxStatus | None
    certificate_reference: str | None
    certificate_path: str | None
    legacy_gross_payment: bool
    eligible: bool
    ineligibility_reason: str | None
    certificate_outstanding_amount: Decimal
    source_updated_at: datetime | None
    native_discharge_id: UUID | None = None


def _invoice(db: Session, invoice_id: UUID) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise _error("invoice_not_found", "Invoice not found.")
    return invoice


def _record(db: Session, invoice_id: UUID) -> WithholdingTaxRecord | None:
    records = list(
        db.scalars(
            select(WithholdingTaxRecord).where(
                WithholdingTaxRecord.source_invoice_id == invoice_id
            )
        )
    )
    if len(records) > 1:
        raise _error(
            "ambiguous_legacy_evidence",
            "Historical withholding evidence requires review.",
        )
    return records[0] if records else None


def _native_account(db: Session, invoice: Invoice) -> BillingAccountObservationV1:
    try:
        from dotmac_billing.contracts import GetBillingAccountByExternalRefV1
        from dotmac_billing.queries import get_billing_account_by_external_ref_v1
    except ImportError as exc:
        raise _error(
            "native_capability_unavailable",
            "Native withholding requires the qualified Billing read capability and genuine Tax policy; no legacy fallback is allowed.",
        ) from exc

    from app.services.billing.starter_policies import resolve_starter_billing_policies

    policies = resolve_starter_billing_policies(db)
    account = get_billing_account_by_external_ref_v1(
        db,
        scope=_SCOPE,
        request=GetBillingAccountByExternalRefV1(
            _SCOPE,
            policies.account_ref(invoice.account_id),
            Currency(invoice.currency, policies.default_minor_units),
        ),
    )
    if account is None:
        raise _error(
            "native_cash_adoption_required",
            "This account has no verified native Billing cash account. Adopt its deposit or opening position through the existing financial owners before issuing with withholding.",
        )
    return account


@dataclass(frozen=True, slots=True)
class NativeWithholdingTaxBindingQuery:
    tenant_id: UUID


@dataclass(frozen=True, slots=True)
class NativeWithholdingTaxBinding:
    tenant_id: UUID
    jurisdiction_id: UUID
    tax_code_id: UUID
    tax_code: str


def query_native_withholding_tax_binding(
    db: Session, query: NativeWithholdingTaxBindingQuery
) -> NativeWithholdingTaxBinding:
    from dotmac_tax.service import get_tax_code

    if query.tenant_id != OPERATOR_TENANT_ID:
        raise _error(
            "native_scope_mismatch",
            "Native Tax binding belongs to the operator tenant.",
        )
    raw = settings_spec.resolve_value(
        db, SettingDomain.billing, "starter_billing_withholding_tax_jurisdiction_id"
    )
    code = settings_spec.resolve_value(
        db, SettingDomain.billing, "starter_billing_withholding_tax_code"
    )
    try:
        jurisdiction_id = UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise _error(
            "native_tax_policy_required",
            "Configure an existing native Tax jurisdiction and withholding code; the legacy rate setting is not Tax determination evidence.",
        ) from exc
    if not isinstance(code, str) or not code.strip():
        raise _error(
            "native_tax_policy_required",
            "Configure the existing native Tax withholding code.",
        )
    try:
        observed = get_tax_code(
            db, tenant_id=query.tenant_id, jurisdiction_id=jurisdiction_id, code=code
        )
    except (ValueError, LookupError) as exc:
        raise _error(
            "native_tax_policy_required",
            "The configured native Tax code is unavailable in the operator tenant.",
        ) from exc
    if observed.status != "active" or observed.tax_kind_code != "withholding":
        raise _error(
            "native_tax_policy_required",
            "The configured native Tax code must be active and have withholding kind.",
        )
    return NativeWithholdingTaxBinding(
        query.tenant_id, observed.jurisdiction_id, observed.tax_code_id, observed.code
    )


def _snapshot(
    db: Session,
    invoice: Invoice,
    *,
    allow_draft: bool = False,
    allow_unselected: bool = False,
) -> InvoiceWithholdingSnapshot:

    if (
        not invoice.is_active
        or invoice.is_proforma
        or (invoice.status == InvoiceStatus.draft and not allow_draft)
    ):
        raise _error(
            "invoice_ineligible", "Withholding requires an active financial invoice."
        )
    if not allow_unselected and not invoice.withholding_tax_selected:
        raise _error(
            "not_selected", "Withholding was not selected on this invoice draft."
        )
    policy = get_customer_withholding_tax_policy(db, account_id=invoice.account_id)
    if not policy.withholding_tax_enabled:
        raise _error(
            "policy_disabled", "This customer is not eligible for withholding."
        )
    if invoice.status == InvoiceStatus.draft:
        _assert_draft_selection_lane(db, invoice)
    try:
        from dotmac_tax.service import preview_tax_policy
    except ImportError as exc:
        raise _error(
            "native_capability_unavailable",
            "Qualified native Tax preview capability is required for withholding.",
        ) from exc

    binding_policy = query_native_withholding_tax_binding(
        db, NativeWithholdingTaxBindingQuery(OPERATOR_TENANT_ID)
    )
    jurisdiction_id = binding_policy.jurisdiction_id
    account = _native_account(db, invoice)
    from dotmac_billing.contracts import WithholdingCashRequirementV1

    try:
        issued_at = invoice.issued_at or datetime.now(UTC)
        binding = (
            str(invoice.id),
            str(Money(to_decimal(invoice.subtotal), account.currency).amount),
            str(Money(to_decimal(invoice.tax_total), account.currency).amount),
            str(Money(to_decimal(invoice.total), account.currency).amount),
            policy.version,
            invoice.withholding_tax_selection_version,
        )
        version = hashlib.sha256(repr(binding).encode()).hexdigest()
        fact = TaxFact(
            jurisdiction_id=jurisdiction_id,
            occurred_on=issued_at.date(),
            fact_kind="invoice",
            recognition_basis_code="invoice_issued",
            transaction_side="withholding",
            base_amount=Money(to_decimal(invoice.subtotal), account.currency),
            source_ref=f"sub-invoice:{invoice.id}",
            source_version=version,
            evidence_ref=f"sub-invoice:{invoice.id}:policy:{policy.version}",
            counterparty_ref=str(invoice.account_id),
        )
        preview = preview_tax_policy(
            db, tenant_id=OPERATOR_TENANT_ID, fact=fact, determined_at=datetime.now(UTC)
        )
        components = tuple(
            c for c in preview.components if c.tax_code_id == binding_policy.tax_code_id
        )
        if len(components) != 1:
            raise _error(
                "native_tax_policy_required",
                "Native Tax did not resolve exactly one configured withholding component.",
            )
        component = components[0]
        quote = WithholdingCashRequirementV1(
            Money(to_decimal(invoice.total), account.currency), component.tax_amount
        )
    except DomainError:
        raise
    except (ValueError, LookupError) as exc:
        raise _error(
            "native_tax_refused",
            "Native Tax refused the configured policy or invoice basis; no cash or withholding was recorded.",
        ) from exc
    rate = (
        component.lines[0].rate.scaleb(2)
        if len(component.lines) == 1 and component.lines[0].rate is not None
        else Decimal("0")
    )
    return InvoiceWithholdingSnapshot(
        invoice.id,
        invoice.account_id,
        invoice.currency,
        quote.invoice_total.amount,
        quote.cash_required.amount,
        quote.withholding_amount.amount,
        rate,
        fact.base_amount.amount,
        Money(to_decimal(invoice.tax_total), account.currency).amount,
        policy.version,
        account.account_id,
        fact,
        preview,
        binding_policy.tax_code_id,
    )


def snapshot_invoice_withholding_for_issue(
    db: Session, invoice_id: UUID
) -> InvoiceWithholdingSnapshot:
    invoice = _invoice(db, invoice_id)
    if invoice.status != InvoiceStatus.draft:
        raise _error("not_draft", "Only a draft can be issued.")
    if (
        resolve_invoice_settlement_amounts(db, invoice_id).total_applied != 0
        or _record(db, invoice_id) is not None
    ):
        raise _error(
            "prior_legacy_settlement",
            "Adopt existing invoice settlement evidence before native issuance.",
        )
    return _snapshot(db, invoice, allow_draft=True)


def preview_invoice_withholding(
    db: Session, request: InvoiceWithholdingPreviewRequest
) -> InvoiceWithholdingPreview:
    if request.existing_payment_id is not None or request.existing_payment_ids:
        raise _error(
            "native_cash_adoption_required",
            "Legacy receipt identities are not native Billing cash. Use the verified deposit or opening adoption owner first.",
        )
    snapshot = _snapshot(db, _invoice(db, request.invoice_id))
    return InvoiceWithholdingPreview(
        snapshot, hashlib.sha256(repr(snapshot).encode()).hexdigest(), None
    )


def stage_invoice_withholding(
    db: Session,
    *,
    snapshot: InvoiceWithholdingSnapshot,
    payment_id: UUID | None = None,
    payment_ids: tuple[UUID, ...] = (),
    payment_proof_id: UUID | None = None,
    context: CommandContext,
    evidence_reference: str | None = None,
) -> UUID:
    from dotmac_billing.commands import AcceptWithholdingDischarge
    from dotmac_billing.contracts import (
        GetCurrentInvoiceReceivableV1,
        WithholdingTaxEvidenceV1,
        WithholdingTaxLineV1,
    )
    from dotmac_billing.queries import get_current_invoice_receivable_v1
    from dotmac_billing.service import accept_withholding_discharge
    from dotmac_tax.service import determine_tax_set

    from app.services.owner_commands import owner_command_active

    if not (
        owner_command_active(db, owner="financial.invoice_withholding")
        or owner_command_active(db, owner="financial.invoice_withholding_issuance")
    ):
        raise _error(
            "native_owner_required",
            "Native withholding must participate in its registered root command.",
        )
    if payment_id is not None or payment_ids or payment_proof_id is not None:
        raise _error(
            "native_cash_adoption_required",
            "This receipt requires verified native cash adoption; withholding cannot create a second cash confirmation.",
        )
    invoice = _invoice(db, snapshot.invoice_id)
    lock_account(db, str(invoice.account_id))
    db.refresh(invoice, with_for_update=True)
    link = db.scalar(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    )
    if link is None or link.billing_account_id != snapshot.billing_account_id:
        raise _error(
            "native_invoice_required",
            "This invoice must be issued by the native Billing owner.",
        )
    fact = get_current_invoice_receivable_v1(
        db,
        scope=_SCOPE,
        request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
    )
    if (
        fact.invoice_total.amount != snapshot.gross_amount
        or fact.subtotal.amount != snapshot.vat_exclusive_amount
        or fact.tax_total.amount != snapshot.vat_amount
    ):
        raise _error(
            "stale_snapshot",
            "Native issued invoice differs from the reviewed withholding basis.",
        )
    if (
        not fact.active
        or fact.withholding_applied.amount != 0
        or not (
            (
                fact.outstanding.amount == snapshot.gross_amount
                and fact.cash_applied.amount == 0
            )
            or (
                fact.outstanding.amount == snapshot.wht_amount
                and fact.cash_applied.amount == snapshot.net_amount
            )
        )
    ):
        raise _error(
            "native_settlement_changed",
            "Native invoice settlement changed after review; no withholding was accepted.",
        )
    result = determine_tax_set(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        fact=snapshot.tax_fact,
        determined_at=datetime.now(UTC),
    )
    components = tuple(
        c for c in result.components if c.tax_code_id == snapshot.tax_code_id
    )
    if len(components) != 1 or components[0].tax_amount.amount != snapshot.wht_amount:
        raise _error("stale_snapshot", "Native Tax policy changed after review.")
    component = components[0]
    evidence = WithholdingTaxEvidenceV1(
        source_owner="dotmac-tax",
        determination_ref=str(result.determination_set_id),
        source_version=result.source_version,
        source_fingerprint=result.source_fingerprint,
        result_fingerprint=result.result_fingerprint,
        evidence_ref=result.evidence_ref,
        source_ref=result.source_ref,
        component_ref=str(component.determination_id),
        rule_ref=str(component.rule_id),
        rule_version=str(component.rule_version),
        treatment_code=component.treatment_code,
        calculation_base_code=component.calculation_base_code,
        inclusive=component.inclusive,
        taxable_basis=component.base_amount,
        amount=component.tax_amount,
        lines=tuple(
            WithholdingTaxLineV1(
                line.sequence, line.taxable_amount, line.rate, line.tax_amount
            )
            for line in component.lines
        ),
        scope=_SCOPE,
        billing_account_id=link.billing_account_id,
        document_id=link.document_id,
        recognition_basis_code=result.recognition_basis_code,
        fact_kind=result.fact_kind,
        transaction_side=result.transaction_side,
        determined_at=result.determined_at,
    )
    discharge = accept_withholding_discharge(
        db,
        scope=_SCOPE,
        command=AcceptWithholdingDischarge(
            scope=_SCOPE,
            billing_account_id=link.billing_account_id,
            document_id=link.document_id,
            idempotency_key=f"sub-invoice-wht:{invoice.id}",
            source_owner="dotmac-sub",
            source_ref=f"invoice:{invoice.id}:withholding",
            claim_ref=evidence_reference or "",
            claimed_amount=component.tax_amount,
            accepted_amount=component.tax_amount,
            tax_evidence=evidence,
            authorization_actor_ref=context.actor,
            authorization_evidence_ref=evidence_reference or "",
            occurred_at=datetime.now(UTC),
        ),
    )
    from sqlalchemy import text

    postgres = db.bind is not None and db.bind.dialect.name == "postgresql"
    if postgres:
        db.execute(
            text(
                "SELECT set_config('app.invoice_native_withholding_projection_invoice', :invoice_id, true)"
            ),
            {"invoice_id": str(invoice.id)},
        )
    try:
        invoice.starter_withholding_discharge_id = discharge.id
        invoice.starter_withholding_tax_set_id = result.determination_set_id
        db.flush()
    finally:
        if postgres and db.is_active:
            db.execute(
                text(
                    "SELECT set_config('app.invoice_native_withholding_projection_invoice', '', true)"
                )
            )
    return discharge.id


_CORRECTION = OwnerCommandDefinition(
    owner="financial.invoice_withholding",
    concern="invoice withholding settlement",
    name="confirm_invoice_withholding",
)


def confirm_invoice_withholding(
    db: Session, command: InvoiceWithholdingConfirm, *, context: CommandContext
) -> UUID:
    def operation() -> UUID:
        if (
            context.scope != "billing:payment:create"
            or not context.actor.startswith("user:")
            or not context.actor.removeprefix("user:").strip()
        ):
            raise _error(
                "invalid_command_context",
                "Native withholding acceptance requires an authorized payment user decision.",
            )
        preview = preview_invoice_withholding(db, command.request)
        if preview.fingerprint != command.preview_fingerprint:
            raise _error("stale_snapshot", "Withholding basis changed; review again.")
        return stage_invoice_withholding(
            db,
            snapshot=preview.snapshot,
            context=context,
            evidence_reference=command.evidence_reference,
        )

    return execute_owner_command(
        db, definition=_CORRECTION, context=context, operation=operation
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingCorrectionChoice:
    payment_id: UUID
    reference: str | None
    amount: Decimal
    paid_at: datetime | None


def correction_choices(
    db: Session, query: InvoiceWithholdingQuery
) -> tuple[InvoiceWithholdingCorrectionChoice, ...]:
    # Legacy payment IDs cannot stand in for native cash backing.
    _invoice(db, query.invoice_id)
    return ()


def query_invoice_withholding(
    db: Session, query: InvoiceWithholdingQuery
) -> InvoiceWithholdingSummary:
    invoice = _invoice(db, query.invoice_id)
    legacy = _record(db, invoice.id)
    snapshot = None
    reason = None
    try:
        snapshot = _snapshot(
            db, invoice, allow_draft=invoice.status == InvoiceStatus.draft
        )
    except DomainError as exc:
        reason = exc.message
    cash = resolve_invoice_settlement_amounts(db, invoice.id).payments_applied
    accepted = legacy.wht_amount if legacy else Decimal("0")
    balance = invoice.balance_due
    status = legacy.status if legacy else None
    record_id = legacy.id if legacy else None
    certificate_reference = legacy.certificate_reference if legacy else None
    updated_at = invoice.updated_at
    if invoice.starter_withholding_discharge_id is not None:
        try:
            from dotmac_billing.contracts import GetCurrentInvoiceReceivableV1
            from dotmac_billing.queries import (
                get_current_invoice_receivable_v1,
                get_withholding_discharge,
            )
        except ImportError as exc:
            raise _error(
                "native_capability_unavailable",
                "The stored native withholding requires its qualified Billing reader; its amounts cannot be reconstructed from local rows.",
            ) from exc

        link = db.scalar(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice.id
            )
        )
        if link is None:
            raise _error(
                "native_projection_unlinked",
                "Native withholding projection lost its source invoice link.",
            )
        current = get_current_invoice_receivable_v1(
            db,
            scope=_SCOPE,
            request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
        )
        discharge = get_withholding_discharge(
            db, scope=_SCOPE, discharge_id=invoice.starter_withholding_discharge_id
        )
        cash, accepted, balance = (
            current.cash_applied.amount,
            current.withholding_applied.amount,
            current.outstanding.amount,
        )
        observation = discharge.certificate_observation
        status = (
            WithholdingTaxStatus(observation.status)
            if observation is not None
            and observation.status in {item.value for item in WithholdingTaxStatus}
            else WithholdingTaxStatus.pending
        )
        record_id = None
        certificate_reference = observation.evidence_ref if observation else None
        if observation is not None:
            updated_at = max(
                value.replace(tzinfo=UTC) if value.tzinfo is None else value
                for value in (updated_at, observation.observed_at)
                if value is not None
            )
    elif legacy is not None:
        cash = legacy.net_amount
    return InvoiceWithholdingSummary(
        invoice.id,
        invoice.currency,
        invoice.total,
        invoice.subtotal,
        invoice.tax_total,
        snapshot.net_amount if snapshot else legacy.net_amount if legacy else None,
        snapshot.wht_amount if snapshot else legacy.wht_amount if legacy else None,
        cash,
        accepted,
        balance,
        record_id,
        status,
        certificate_reference,
        legacy.certificate_path if legacy else None,
        legacy is not None,
        snapshot is not None,
        reason,
        accepted
        if status in {WithholdingTaxStatus.pending, WithholdingTaxStatus.certified}
        else Decimal("0"),
        updated_at,
        invoice.starter_withholding_discharge_id,
    )


# Compatibility reads expose source observations only; no local AR formula uses them.
def withholding_cash_payment_ids(db: Session, record_id: UUID) -> tuple[UUID, ...]:
    record = db.get(WithholdingTaxRecord, record_id)
    return (
        (record.payment_id,)
        if record is not None and record.payment_id is not None
        else ()
    )


def record_for_cash_payment(
    db: Session, payment_id: UUID, invoice_id: UUID
) -> WithholdingTaxRecord | None:
    return db.scalar(
        select(WithholdingTaxRecord).where(
            WithholdingTaxRecord.payment_id == payment_id,
            WithholdingTaxRecord.source_invoice_id == invoice_id,
        )
    )


@dataclass(frozen=True, slots=True)
class InvoiceWithholdingSelection:
    invoice_id: UUID
    selected: bool
    version: int
    eligible: bool
    ineligibility_reason: str | None
    editable: bool


@dataclass(frozen=True, slots=True)
class SetDraftInvoiceWithholdingSelection:
    invoice_id: UUID
    selected: bool
    expected_version: int


def _assert_draft_selection_lane(db: Session, invoice: Invoice) -> None:
    from app.services.billing.starter_issuance import (
        AdministrativeEligibilityPolicy,
        eligibility_reason,
    )

    if (
        eligibility_reason(
            db,
            invoice,
            administrative_policy=AdministrativeEligibilityPolicy.FUTURE_CATALOGUE,
        )
        is not None
    ):
        raise _error(
            "native_tax_evidence_required",
            "This draft requires an eligible Starter issuance lane and native Tax policy before withholding can be selected.",
        )


def query_invoice_withholding_selection(
    db: Session, query: InvoiceWithholdingQuery
) -> InvoiceWithholdingSelection:
    invoice = _invoice(db, query.invoice_id)
    reason = None
    try:
        if invoice.status != InvoiceStatus.draft:
            raise _error(
                "selection_immutable",
                "Withholding selection cannot change after invoice issue.",
            )
        _assert_draft_selection_lane(db, invoice)
        _snapshot(db, invoice, allow_draft=True, allow_unselected=True)
    except DomainError as exc:
        reason = exc.message
    return InvoiceWithholdingSelection(
        invoice.id,
        bool(invoice.withholding_tax_selected),
        invoice.withholding_tax_selection_version,
        reason is None,
        reason,
        bool(invoice.is_active and invoice.status == InvoiceStatus.draft),
    )


_SELECTION_COMMAND = OwnerCommandDefinition(
    owner="financial.invoice_withholding",
    concern="draft invoice withholding selection",
    name="set_draft_invoice_withholding_selection",
)


def set_draft_invoice_withholding_selection(
    db: Session,
    command: SetDraftInvoiceWithholdingSelection,
    *,
    context: CommandContext,
) -> InvoiceWithholdingSelection:
    def operation() -> InvoiceWithholdingSelection:
        if context.scope != "billing:invoice:update" or (
            not context.actor.startswith("user:")
            or not context.actor.removeprefix("user:").strip()
        ):
            raise _error(
                "invalid_command_context",
                "Withholding selection requires an authenticated invoice-update decision.",
            )
        if type(command.selected) is not bool or command.expected_version < 0:
            raise _error(
                "selection_invalid", "Withholding selection and version are invalid."
            )
        invoice = _invoice(db, command.invoice_id)
        lock_account(db, str(invoice.account_id))
        db.refresh(invoice, with_for_update=True)
        if invoice.status != InvoiceStatus.draft or not invoice.is_active:
            raise _error(
                "selection_immutable",
                "Withholding selection cannot change after invoice issue.",
            )
        if invoice.withholding_tax_selection_version != command.expected_version:
            raise _error(
                "stale_selection", "Withholding selection changed; reload this invoice."
            )
        if command.selected:
            _assert_draft_selection_lane(db, invoice)
            _snapshot(db, invoice, allow_draft=True, allow_unselected=True)
            if (
                invoice.starter_withholding_discharge_id is not None
                or _record(db, invoice.id) is not None
            ):
                raise _error(
                    "already_accepted",
                    "This invoice already has accepted withholding evidence.",
                )
        if bool(invoice.withholding_tax_selected) == command.selected:
            return query_invoice_withholding_selection(
                db, InvoiceWithholdingQuery(invoice.id)
            )
        version = invoice.withholding_tax_selection_version + 1
        permit = InvoiceWithholdingSelectionWritePermit(
            invoice.id, command.selected, version
        )
        from sqlalchemy import text

        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(
                text(
                    "SELECT set_config('app.invoice_withholding_selection_invoice', :invoice_id, true)"
                ),
                {"invoice_id": str(invoice.id)},
            )
        db.info[INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY] = permit
        try:
            invoice.withholding_tax_selected = command.selected
            invoice.withholding_tax_selection_version = version
            db.flush()
        finally:
            db.info.pop(INVOICE_WITHHOLDING_SELECTION_PERMIT_KEY, None)
            if (
                db.is_active
                and db.bind is not None
                and db.bind.dialect.name == "postgresql"
            ):
                db.execute(
                    text(
                        "SELECT set_config('app.invoice_withholding_selection_invoice', '', true)"
                    )
                )
        from app.models.audit import AuditActorType
        from app.services.audit_adapter import stage_audit_event

        stage_audit_event(
            db,
            action="set_draft_invoice_withholding_selection",
            entity_type="invoice",
            entity_id=str(invoice.id),
            actor_type=AuditActorType.user,
            actor_id=context.actor.removeprefix("user:"),
            request_id=str(context.correlation_id),
            metadata={
                "owner": "financial.invoice_withholding",
                "selected": command.selected,
                "selection_version": version,
                "command_id": str(context.command_id),
            },
        )
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.invoice_withholding_selection_changed,
            {
                "schema_version": 1,
                "aggregate_type": "invoice",
                "aggregate_id": str(invoice.id),
                "aggregate_version": str(version),
                "invoice_id": str(invoice.id),
                "selected": command.selected,
                "selection_version": version,
                "command_id": str(context.command_id),
                "correlation_id": str(context.correlation_id),
            },
            actor=context.actor,
        )
        return query_invoice_withholding_selection(
            db, InvoiceWithholdingQuery(invoice.id)
        )

    return execute_owner_command(
        db, definition=_SELECTION_COMMAND, context=context, operation=operation
    )


def snapshot_invoice_withholding_for_transfer(
    db: Session, query: InvoiceWithholdingQuery
) -> InvoiceWithholdingSnapshot:
    _invoice(db, query.invoice_id)
    raise _error(
        "native_transfer_adoption_required",
        "Reduced bank-transfer instructions require the native receipt/proof adoption owner. Use the actual account cash deposit and reviewed invoice Issue workflow; do not create a second payment.",
    )


def accepted_invoice_withholding_total(db: Session, invoice_id: UUID) -> Decimal:
    """Read source evidence for documentary immutability; never an AR formula."""
    return query_invoice_withholding(
        db, InvoiceWithholdingQuery(invoice_id)
    ).accepted_wht_amount
