"""Flush-only native proof participant: frozen Tax expectation is never cash.

The financial.payment_proofs root owns trust, account/proof locks and transaction.
This bounded lane accepts one complete original invoice and every positive frozen
component once. Billing owns discharge and reversal, Payments owns actual cash.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypeAlias
from uuid import UUID

from dotmac_billing import (
    GetCurrentReceivablePositionV1,
    InvoiceDocumentFactV2,
    InvoiceDocumentFactV3,
    get_current_receivable_position,
)
from dotmac_billing.commands import AcceptWithholdingDischarge
from dotmac_billing.contracts import (
    WithholdingTaxEvidenceV1,
    WithholdingTaxEvidenceV2,
    WithholdingTaxLineV1,
)
from dotmac_billing.service import accept_withholding_discharge
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import fingerprint_of
from dotmac_kernel.money import Currency, Money
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, StarterInvoiceLink, TopupIntent
from app.models.payment_proof import PaymentProof
from app.services.billing.starter_positions import (
    starter_invoice_fact,
)
from app.services.billing.starter_settlement import StarterCashSettlement
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

NativeInvoiceFact: TypeAlias = InvoiceDocumentFactV2 | InvoiceDocumentFactV3
NativeWithholdingEvidence: TypeAlias = (
    WithholdingTaxEvidenceV1 | WithholdingTaxEvidenceV2
)

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


def _proof_cash_allocation_source_ref(proof_id: UUID) -> str:
    """Immutable proof identity, independent of editable settlement policy."""
    return f"proof:{proof_id}:cash-allocation"


class NativeWithholdingProofError(DomainError, ValueError):
    """A native claim is incomplete, changed, reused or outside the supported lane."""


def _error(message: str) -> NativeWithholdingProofError:
    return NativeWithholdingProofError(
        code="financial.payment_proofs.native_withholding_refused", message=message
    )


@dataclass(frozen=True, slots=True)
class NativeWithholdingClaim:
    component_id: UUID
    claimed_amount: Decimal
    accepted_amount: Decimal


@dataclass(frozen=True, slots=True)
class NativeWithholdingReview:
    claims: tuple[NativeWithholdingClaim, ...]
    acceptance_evidence_ref: str

    def __post_init__(self) -> None:
        if (
            not self.acceptance_evidence_ref.strip()
            or len(self.acceptance_evidence_ref) > 255
        ):
            raise _error("Explicit finance acceptance evidence is required")
        if not self.claims or len({claim.component_id for claim in self.claims}) != len(
            self.claims
        ):
            raise _error(
                "Each positive frozen withholding component needs one explicit claim and acceptance"
            )
        for claim in self.claims:
            if not isinstance(claim.component_id, UUID) or any(
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                for value in (claim.claimed_amount, claim.accepted_amount)
            ):
                raise _error(
                    "Withholding component amounts must be explicit positive exact values"
                )

    def serialized(self) -> dict[str, object]:
        return {
            "acceptance_evidence_ref": self.acceptance_evidence_ref,
            "claims": [
                {
                    "component_id": str(claim.component_id),
                    "claimed_amount": str(claim.claimed_amount.normalize()),
                    "accepted_amount": str(claim.accepted_amount.normalize()),
                }
                for claim in sorted(
                    self.claims, key=lambda claim: str(claim.component_id)
                )
            ],
        }


def frozen_components(
    fact: NativeInvoiceFact, *, billing_account_id: UUID
) -> tuple[NativeWithholdingEvidence, ...]:
    """Lossless mapping of the original Tax set/component and ordered bands."""
    if isinstance(fact, InvoiceDocumentFactV3):
        return tuple(
            WithholdingTaxEvidenceV2(
                determination=entry.determination,
                prior_determinations=entry.prior_determinations,
                source_owner="dotmac-tax",
                determination_ref=str(determination.determination_set_id),
                source_version=determination.source_version,
                source_fingerprint=determination.source_fingerprint,
                result_fingerprint=determination.result_fingerprint,
                evidence_ref=determination.evidence_ref,
                source_ref=determination.source_ref,
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
                        sequence=line.sequence,
                        taxable_amount=line.taxable_amount,
                        rate=line.rate,
                        tax_amount=line.tax_amount,
                    )
                    for line in component.lines
                ),
                scope=_SCOPE,
                billing_account_id=billing_account_id,
                document_id=fact.invoice_id,
                recognition_basis_code=determination.recognition_basis_code,
                fact_kind=determination.fact_kind,
                transaction_side=determination.transaction_side,
                determined_at=determination.determined_at,
            )
            for entry in fact.tax_evidence
            if entry.purpose == "expected_withholding"
            for determination in (entry.determination,)
            for component in determination.components
            if component.tax_amount.amount > 0
        )
    return tuple(
        WithholdingTaxEvidenceV1(
            source_owner="dotmac-tax",
            determination_ref=str(determination.determination_set_id),
            source_version=determination.source_version,
            source_fingerprint=determination.source_fingerprint,
            result_fingerprint=determination.result_fingerprint,
            evidence_ref=determination.evidence_ref,
            source_ref=determination.source_ref,
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
                    sequence=line.sequence,
                    taxable_amount=line.taxable_amount,
                    rate=line.rate,
                    tax_amount=line.tax_amount,
                )
                for line in component.lines
            ),
            scope=_SCOPE,
            billing_account_id=billing_account_id,
            document_id=fact.invoice_id,
            recognition_basis_code=determination.recognition_basis_code,
            fact_kind=determination.fact_kind,
            transaction_side=determination.transaction_side,
            determined_at=determination.determined_at,
        )
        for entry in fact.tax_evidence
        if entry.purpose == "expected_withholding"
        for determination in (entry.determination,)
        for component in determination.components
        if component.tax_amount.amount > 0
    )


class NativeWithholdingComponentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    component_id: UUID
    determination_ref: UUID
    result_fingerprint: str
    amount: Decimal = Field(gt=0)


class NativeWithholdingSnapshot(BaseModel):
    """Typed persisted checkout identity; JSON exists only at the storage edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[2] = 2
    currency: str
    source_invoice_id: UUID
    billing_account_id: UUID
    document_id: UUID
    fact_version: int = Field(gt=0)
    fact_fingerprint: str
    gross_amount: Decimal = Field(gt=0)
    net_amount: Decimal = Field(gt=0)
    withholding_tax_amount: Decimal = Field(gt=0)
    components: tuple[NativeWithholdingComponentSnapshot, ...]

    @model_validator(mode="after")
    def complete_original_invoice(self) -> NativeWithholdingSnapshot:
        if (
            not self.components
            or len({item.component_id for item in self.components})
            != len(self.components)
            or sum((item.amount for item in self.components), Decimal(0))
            != self.withholding_tax_amount
            or self.net_amount + self.withholding_tax_amount != self.gross_amount
        ):
            raise ValueError(
                "Native withholding requires complete unique components and exact original cash reconciliation"
            )
        return self

    def serialized(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class NativeWithholdingReceipt(BaseModel):
    """Opaque native owner identities returned without a legacy Payment id."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    schema_version: Literal[2]
    payment_proof_id: UUID
    confirmation_id: UUID
    settlement_id: UUID
    allocation_posting_group_id: UUID
    discharge_ids: tuple[UUID, ...]
    actual_amount: Decimal
    fact_fingerprint: str

    def serialized(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def read_native_snapshot(value: object) -> NativeWithholdingSnapshot:
    """Validate JSON at the persistence boundary before using domain values."""
    try:
        return NativeWithholdingSnapshot.model_validate(value)
    except ValueError as exc:
        raise _error(
            "Persisted native withholding checkout evidence is invalid"
        ) from exc


def native_snapshot(
    db: Session, *, invoice: Invoice, link: StarterInvoiceLink
) -> NativeWithholdingSnapshot:
    fact = starter_invoice_fact(db, invoice)
    if (
        not isinstance(fact, (InvoiceDocumentFactV2, InvoiceDocumentFactV3))
        or fact.document_state != "issued"
    ):
        raise _error(
            "Native withholding requires the original issued V2 or V3 invoice fact"
        )
    components = frozen_components(fact, billing_account_id=link.billing_account_id)
    if not components or fact.expected_withholding.amount <= 0:
        raise _error("Invoice has no positive frozen withholding expectation")
    net = fact.grand_total.amount - fact.expected_withholding.amount
    if net <= 0 or invoice.balance_due != fact.grand_total.amount:
        raise _error(
            "Native withholding supports only an unadjusted invoice with positive net cash"
        )
    # The public current reader retains Billing's account lock through the
    # caller transaction. One monetary posting version proves there have been
    # no offsetting cash/reversal/credit adjustments hidden by a net balance.
    current = get_current_receivable_position(
        db,
        scope=_SCOPE,
        request=GetCurrentReceivablePositionV1(
            scope=_SCOPE,
            billing_account_id=link.billing_account_id,
            currency=Currency(fact.currency, fact.minor_units),
        ),
    )
    links = tuple(
        db.scalars(
            select(StarterInvoiceLink.invoice_id).where(
                StarterInvoiceLink.billing_account_id == link.billing_account_id
            )
        )
    )
    if (
        links != (invoice.id,)
        or current.source_version != 1
        or current.position is None
        or current.position.collectible_receivable != fact.grand_total
    ):
        raise _error(
            "Native withholding supports exactly one original native invoice with no other Billing monetary history on the account"
        )
    return _snapshot_from_fact(invoice=invoice, link=link, fact=fact)


def _snapshot_from_fact(
    *, invoice: Invoice, link: StarterInvoiceLink, fact: NativeInvoiceFact
) -> NativeWithholdingSnapshot:
    """Rebuild checkout evidence from the exact original fact without admission."""
    components = frozen_components(fact, billing_account_id=link.billing_account_id)
    net = fact.grand_total.amount - fact.expected_withholding.amount
    return NativeWithholdingSnapshot(
        currency=fact.currency,
        source_invoice_id=invoice.id,
        billing_account_id=link.billing_account_id,
        document_id=link.document_id,
        fact_version=fact.fact_version,
        fact_fingerprint=fingerprint_of(asdict(fact)),
        gross_amount=fact.grand_total.amount,
        net_amount=net,
        withholding_tax_amount=fact.expected_withholding.amount,
        components=tuple(
            NativeWithholdingComponentSnapshot(
                component_id=UUID(item.component_ref),
                determination_ref=UUID(item.determination_ref),
                result_fingerprint=item.result_fingerprint,
                amount=item.amount.amount,
            )
            for item in components
        ),
    )


def validate_native_withholding_replay(
    db: Session,
    *,
    proof: PaymentProof,
    intent: TopupIntent,
    review: NativeWithholdingReview,
    actor_ref: str,
    receipt_value: object,
) -> NativeWithholdingReceipt:
    """Verify original immutable bindings, never today's balance or Tax policy."""
    from dotmac_billing.errors import BillingError
    from dotmac_billing.queries import (
        get_allocation_accounting_fact_v1,
        get_withholding_discharge,
    )
    from dotmac_payments import (
        GetConfirmedCashV1,
        GetPaymentIntentV1,
        PaymentError,
        get_confirmed_cash_v1,
        get_payment_intent_v1,
    )

    from app.services.billing.starter_positions import starter_invoice_link

    snapshot = read_native_snapshot((intent.metadata_ or {}).get("withholding_tax"))
    receipt = NativeWithholdingReceipt.model_validate(receipt_value)
    invoice = (
        db.get(Invoice, intent.invoice_id) if intent.invoice_id is not None else None
    )
    link = starter_invoice_link(db, invoice.id) if invoice is not None else None
    if (
        invoice is None
        or link is None
        or proof.account_id is None
        or invoice.account_id != proof.account_id
        or intent.account_id != proof.account_id
        or invoice.currency != proof.currency
        or intent.currency != proof.currency
        or str((intent.metadata_ or {}).get("invoice_id")) != str(invoice.id)
    ):
        raise _error(
            "Native replay invoice and account identities differ from accepted proof"
        )
    fact = starter_invoice_fact(db, invoice)
    if not isinstance(fact, (InvoiceDocumentFactV2, InvoiceDocumentFactV3)):
        raise _error(
            "Native replay requires the original immutable V2 or V3 invoice fact"
        )
    original = _snapshot_from_fact(invoice=invoice, link=link, fact=fact)
    if (
        snapshot != original
        or receipt.fact_fingerprint != original.fact_fingerprint
        or receipt.payment_proof_id != proof.id
        or receipt.actual_amount != proof.verified_amount
        or receipt.actual_amount != original.net_amount
        or intent.requested_amount != original.net_amount
    ):
        raise _error(
            "Native replay frozen checkout and acceptance identities differ from original invoice fact"
        )
    try:
        cash = get_confirmed_cash_v1(
            db, scope=_SCOPE, query=GetConfirmedCashV1(receipt.confirmation_id)
        )
        payment_intent = get_payment_intent_v1(
            db, scope=_SCOPE, query=GetPaymentIntentV1(reference=intent.reference)
        )
    except PaymentError as exc:
        raise _error("Native replay original cash evidence is unavailable") from exc
    if (
        payment_intent is None
        or cash.intent_id != payment_intent.intent_id
        or payment_intent.payer_reference != str(proof.account_id)
        or payment_intent.target_reference != str(invoice.id)
        or cash.external_reference != f"proof:{proof.id}"
        or cash.confirmed.amount != original.net_amount
        or cash.confirmed.currency != fact.grand_total.currency
    ):
        raise _error(
            "Native replay cash identities differ from originally accepted proof"
        )
    try:
        allocation_fact = get_allocation_accounting_fact_v1(
            db, scope=_SCOPE, posting_group_id=receipt.allocation_posting_group_id
        )
    except BillingError as exc:
        raise _error(
            "Native replay original allocation evidence is unavailable"
        ) from exc
    if (
        allocation_fact.scope != _SCOPE
        or allocation_fact.fact_version != 1
        or allocation_fact.posting_group_id != receipt.allocation_posting_group_id
        or allocation_fact.billing_account_id != link.billing_account_id
        or allocation_fact.source_system != "dotmac-billing"
        or allocation_fact.source_authority != "internal"
        or allocation_fact.effect_kind != "allocation"
        or allocation_fact.reverses_fact_id is not None
        or allocation_fact.source_ref != _proof_cash_allocation_source_ref(proof.id)
        or len(allocation_fact.allocations) != 1
    ):
        raise _error(
            "Native replay allocation posting identity differs from original cash allocation"
        )
    allocation = allocation_fact.allocations[0]
    if (
        allocation.settlement_id != receipt.settlement_id
        or allocation.document_id != link.document_id
        or allocation.effect_kind != "allocation"
        or allocation.offsets_allocation_id is not None
        or allocation.amount_delta != cash.confirmed
        or len(allocation_fact.effects) != 2
        or {item.lane for item in allocation_fact.effects}
        not in ({"receivable", "available_credit"}, {"receivable", "prepaid_funding"})
        or any(
            item.amount_delta.currency != cash.confirmed.currency
            or item.amount_delta.amount != -cash.confirmed.amount
            for item in allocation_fact.effects
        )
    ):
        raise _error(
            "Native replay allocation effects differ from original actual-cash amount and invoice"
        )
    evidence = {
        item.component_ref: item
        for item in frozen_components(fact, billing_account_id=link.billing_account_id)
    }
    claims = {str(item.component_id): item for item in review.claims}
    if (
        set(claims) != set(evidence)
        or len(receipt.discharge_ids) != len(evidence)
        or len(set(receipt.discharge_ids)) != len(evidence)
    ):
        raise _error(
            "Native replay does not contain the exact originally accepted components"
        )
    seen: set[str] = set()
    for discharge_id in receipt.discharge_ids:
        try:
            effect = get_withholding_discharge(
                db, scope=_SCOPE, discharge_id=discharge_id
            )
        except BillingError as exc:
            raise _error(
                "Native replay original discharge evidence is unavailable"
            ) from exc
        component_ref = effect.tax_evidence.component_ref
        if (
            component_ref not in evidence
            or component_ref in seen
            or effect.tax_evidence != evidence[component_ref]
            or effect.document_id != link.document_id
            or effect.settlement_id != receipt.settlement_id
            or effect.claim_ref != f"proof:{proof.id}:claim:{component_ref}"
            or effect.claimed_amount.amount != claims[component_ref].claimed_amount
            or effect.accepted_delta.amount != claims[component_ref].accepted_amount
            or effect.authorization_actor_ref != actor_ref
            or effect.authorization_evidence_ref != review.acceptance_evidence_ref
            or effect.reverses_discharge_id is not None
        ):
            raise _error(
                "Native replay discharge differs from original Tax component acceptance"
            )
        seen.add(component_ref)
    return receipt


@dataclass(slots=True)
class NativeWithholdingParticipant:
    invoice: Invoice
    link: StarterInvoiceLink
    proof_id: UUID
    intent: TopupIntent
    review: NativeWithholdingReview
    actor_ref: str
    occurred_at: datetime
    evidence: tuple[NativeWithholdingEvidence, ...]
    discharge_ids: tuple[UUID, ...] = ()

    @property
    def allocation_source_ref(self) -> str:
        return _proof_cash_allocation_source_ref(self.proof_id)

    def stage(self, db: Session, *, cash: StarterCashSettlement) -> None:
        claims = {str(claim.component_id): claim for claim in self.review.claims}
        ids = []
        for item in self.evidence:
            claim = claims[item.component_ref]
            discharge = accept_withholding_discharge(
                db,
                scope=_SCOPE,
                command=AcceptWithholdingDischarge(
                    scope=_SCOPE,
                    billing_account_id=self.link.billing_account_id,
                    document_id=self.link.document_id,
                    idempotency_key=f"proof:{self.proof_id}:wht:{item.component_ref}",
                    source_owner="dotmac-sub.payment-proofs",
                    source_ref=f"proof:{self.proof_id}:{item.component_ref}",
                    claim_ref=f"proof:{self.proof_id}:claim:{item.component_ref}",
                    claimed_amount=Money(claim.claimed_amount, item.amount.currency),
                    accepted_amount=Money(claim.accepted_amount, item.amount.currency),
                    tax_evidence=item,
                    authorization_actor_ref=self.actor_ref,
                    authorization_evidence_ref=self.review.acceptance_evidence_ref,
                    occurred_at=self.occurred_at,
                    settlement_id=cash.settlement_id,
                ),
            )
            ids.append(discharge.id)
        self.discharge_ids = tuple(ids)


def prepare_native_withholding(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    proof_id: UUID,
    intent: TopupIntent,
    review: NativeWithholdingReview | None,
    actor_ref: str,
    occurred_at: datetime,
    actual_cash: Decimal,
) -> NativeWithholdingParticipant:
    if review is None or not actor_ref.strip():
        raise _error(
            "Explicit component claims and finance acceptance evidence are required"
        )
    snapshot = native_snapshot(db, invoice=invoice, link=link)
    if (
        snapshot.serialized() != (intent.metadata_ or {}).get("withholding_tax")
        or actual_cash != snapshot.net_amount
    ):
        raise _error(
            "Proof cash and original invoice evidence must exactly match the issued transfer intent"
        )
    fact = starter_invoice_fact(db, invoice)
    assert isinstance(fact, (InvoiceDocumentFactV2, InvoiceDocumentFactV3))
    evidence = frozen_components(fact, billing_account_id=link.billing_account_id)
    claims = {str(claim.component_id): claim for claim in review.claims}
    if set(claims) != {item.component_ref for item in evidence} or any(
        claims[item.component_ref].claimed_amount != item.amount.amount
        or claims[item.component_ref].accepted_amount != item.amount.amount
        for item in evidence
    ):
        raise _error(
            "Every positive original component must be claimed and explicitly accepted in full; partial or changed claims are unsupported"
        )
    return NativeWithholdingParticipant(
        invoice=invoice,
        link=link,
        proof_id=proof_id,
        intent=intent,
        review=review,
        actor_ref=actor_ref,
        occurred_at=occurred_at,
        evidence=evidence,
    )


def open_native_proof_intent(db: Session, *, intent: TopupIntent) -> None:
    """Register the exact persisted checkout binding with the Payments owner."""
    from dotmac_kernel.money import Currency
    from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent

    snapshot = (intent.metadata_ or {}).get("withholding_tax")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != 2
        or intent.account_id is None
    ):
        raise _error("Native proof checkout has no exact frozen invoice binding")
    open_payment_intent(
        db,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=intent.reference,
            payer_reference=str(intent.account_id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(snapshot["source_invoice_id"]),
            requested=Money(intent.requested_amount, Currency(intent.currency, 2)),
            provider_type=intent.provider_type,
            channel=intent.channel or "customer_selfcare",
            opened_at=intent.created_at,
            expires_at=intent.expires_at,
        ),
    )
