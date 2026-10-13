"""Complete current-snapshot source census under the invoice domain.

The caller supplies a PostgreSQL READ ONLY, REPEATABLE READ/SERIALIZABLE
transaction. No mutable historical reconstruction or import is performed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    PaymentRefund,
    PaymentReversal,
)
from app.models.billing_contract import BillingRecordAuthority
from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    CustomerSubledgerOpeningPosition,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidOpeningFundingConsumption,
)
from app.models.subscriber import Subscriber
from app.services.billing._common import resolve_invoice_settlement_amounts
from app.services.billing.customer_subledger import (
    authority_cutover,
    resolve_position_evidence_at,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreview,
    CaptureEvidence,
    ExceptionCode,
    ExclusionReason,
    FinancialSourceEvidence,
    InvoiceLineEvidence,
    InvoiceOpeningEvidence,
    LaneEvidence,
    OpeningControl,
    OpeningException,
    OpeningExclusion,
    PositionEffectEvidence,
    PreviewBillingOpeningExportQuery,
    SettlementEvidence,
    SettlementKind,
    SourceKind,
)
from app.services.domain_errors import DomainError

OWNER = "financial.invoices.opening_export"
ZERO = Decimal("0")


class BillingOpeningExportError(DomainError):
    """The requested census cannot be captured truthfully."""


def _fail(code: str, message: str) -> BillingOpeningExportError:
    return BillingOpeningExportError(code=f"{OWNER}.{code}", message=message)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize()) if value else "0"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def _fingerprint(preview: BillingOpeningExportPreview) -> str:
    # The capture envelope identifies the actual observation. Content identity
    # excludes the clock/snapshot label so repeated unchanged captures compare.
    payload = asdict(preview)
    payload.pop("content_fingerprint")
    capture = payload["capture"]
    if not isinstance(capture, dict):  # pragma: no cover - dataclass invariant
        raise TypeError("Invalid capture")
    capture.pop("snapshot_id")
    capture.pop("captured_at")
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=_json_value
        ).encode()
    ).hexdigest()


def _finite(*amounts: Decimal | None) -> None:
    if any(amount is not None and not amount.is_finite() for amount in amounts):
        raise _fail(
            "nonfinite_source",
            "Non-finite monetary evidence prevents finite controls; capture failed without substitution.",
        )


def _settlement_ledger_matches(
    settlement: SettlementEvidence,
    ledger: FinancialSourceEvidence,
    *,
    consumption: bool = False,
) -> bool:
    """Check structural affinity only; canonical owners still resolve money."""
    negative = settlement.amount < ZERO
    if settlement.kind == SettlementKind.opening_funding:
        expected_source, expected_direction = "adjustment", "debit"
    else:
        expected_source = (
            "payment" if settlement.kind == SettlementKind.payment else "credit_note"
        )
        expected_direction = "debit" if negative else "credit"
    if consumption:
        expected_source = (
            "other" if settlement.kind == SettlementKind.payment else "credit_note"
        )
        expected_direction = "credit" if negative else "debit"
    expected_payment = (
        settlement.source_id if settlement.kind == SettlementKind.payment else None
    )
    return (
        ledger.active
        and ledger.invoice_id == (None if consumption else settlement.invoice_id)
        and ledger.payment_id == expected_payment
        and ledger.source_kind == expected_source
        and ledger.status == expected_direction
        and ledger.amount == abs(settlement.amount)
        and (negative or ledger.reverses_id is None)
    )


def _capture(db: Session, query: PreviewBillingOpeningExportQuery) -> CaptureEvidence:
    if (
        re.fullmatch(r"[0-9a-f]{40}", query.source_repository_sha) is None
        or query.population_version != "sub-billing-opening-census-v1"
        or query.economic_cutoff.tzinfo is None
        or len(dict(query.currency_precisions)) != len(query.currency_precisions)
        or any(
            not re.fullmatch(r"[A-Z]{3}", c) or p != 2
            for c, p in query.currency_precisions
        )
    ):
        raise _fail(
            "invalid_query",
            "Declare exact repository SHA, population version, aware cutoff and source two-decimal currency evidence.",
        )
    if not db.in_transaction() or db.new or db.dirty or db.deleted:
        raise _fail(
            "snapshot_required",
            "Supply a clean caller-owned read-only snapshot transaction.",
        )
    if db.get_bind().dialect.name != "postgresql":
        raise _fail("snapshot_required", "PostgreSQL snapshot evidence is required.")
    row = db.execute(
        text(
            "SELECT current_setting('transaction_isolation'), "
            "current_setting('transaction_read_only'), pg_current_snapshot()::text, clock_timestamp()"
        )
    ).one()
    if row[0] not in {"repeatable read", "serializable"} or row[1] != "on":
        raise _fail(
            "snapshot_required",
            "Caller transaction must already be READ ONLY and REPEATABLE READ or SERIALIZABLE.",
        )
    if query.economic_cutoff > row[3]:
        raise _fail("invalid_query", "Economic cutoff cannot be later than capture.")
    heads = tuple(
        db.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).scalars()
    )
    if not heads:
        raise _fail(
            "schema_evidence_missing", "Migration-chain schema heads are required."
        )
    return CaptureEvidence(query, heads, row[2], row[3], row[0], True)


def _sources(db: Session) -> tuple[FinancialSourceEvidence, ...]:
    result: list[FinancialSourceEvidence] = []
    for r in db.execute(
        select(
            Payment.id,
            Payment.account_id,
            Payment.currency,
            Payment.amount,
            Payment.refunded_amount,
            Payment.status,
            Payment.is_active,
            Payment.created_at,
            Payment.updated_at,
            Payment.paid_at,
        ).order_by(Payment.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.payment,
                r.id,
                r.account_id,
                r.currency,
                r.amount,
                r.refunded_amount,
                r.status.value,
                r.is_active,
                None,
                r.id,
                None,
                None,
                r.created_at,
                r.updated_at,
                r.paid_at,
            )
        )
    for r in db.execute(
        select(
            CreditNote.id,
            CreditNote.account_id,
            CreditNote.currency,
            CreditNote.total,
            CreditNote.applied_total,
            CreditNote.status,
            CreditNote.is_active,
            CreditNote.invoice_id,
            CreditNote.funding_ledger_entry_id,
            CreditNote.void_ledger_entry_id,
            CreditNote.created_at,
            CreditNote.updated_at,
            CreditNote.issued_at,
        ).order_by(CreditNote.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.credit_note,
                r.id,
                r.account_id,
                r.currency,
                r.total,
                r.applied_total,
                r.status.value,
                r.is_active,
                r.invoice_id,
                None,
                r.funding_ledger_entry_id,
                r.void_ledger_entry_id,
                r.created_at,
                r.updated_at,
                r.issued_at,
            )
        )
    for r in db.execute(
        select(
            LedgerEntry.id,
            LedgerEntry.account_id,
            LedgerEntry.currency,
            LedgerEntry.amount,
            LedgerEntry.entry_type,
            LedgerEntry.source,
            LedgerEntry.is_active,
            LedgerEntry.invoice_id,
            LedgerEntry.payment_id,
            LedgerEntry.reversal_of_entry_id,
            LedgerEntry.created_at,
            LedgerEntry.updated_at,
            LedgerEntry.effective_date,
        ).order_by(LedgerEntry.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.ledger,
                r.id,
                r.account_id,
                r.currency,
                r.amount,
                None,
                r.entry_type.value,
                r.is_active,
                r.invoice_id,
                r.payment_id,
                r.id,
                r.reversal_of_entry_id,
                r.created_at,
                r.updated_at,
                r.effective_date,
                source_kind=r.source.value if r.source else None,
            )
        )
    for r in db.execute(
        select(
            CustomerPostingGroup.id,
            CustomerPostingGroup.account_id,
            CustomerPostingGroup.currency,
            CustomerPostingGroup.authority,
            CustomerPostingGroup.command_kind,
            CustomerPostingGroup.reverses_group_id,
            CustomerPostingGroup.recorded_at,
            CustomerPostingGroup.occurred_at,
            CustomerPostingGroup.source_kind,
            CustomerPostingGroup.source_id,
        ).order_by(CustomerPostingGroup.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.posting_group,
                r.id,
                r.account_id,
                r.currency,
                None,
                None,
                r.command_kind.value,
                True,
                None,
                None,
                None,
                r.reverses_group_id,
                r.recorded_at,
                None,
                r.occurred_at,
                r.authority,
                r.source_kind,
                r.source_id,
            )
        )
    for model, kind in (
        (PaymentRefund, SourceKind.refund),
        (PaymentReversal, SourceKind.reversal),
    ):
        for r in db.execute(
            select(
                model.id,
                model.payment_id,
                model.amount,
                model.currency,
                model.origin,
                model.ledger_entry_id,
                model.created_at,
                model.preview_fingerprint,
                model.credit_consumption_ledger_entry_id,
                Payment.account_id,
            )
            .outerjoin(Payment, Payment.id == model.payment_id)
            .order_by(model.id)
        ):
            result.append(
                FinancialSourceEvidence(
                    kind,
                    r.id,
                    r.account_id,
                    r.currency,
                    r.amount,
                    None,
                    r.origin.value,
                    True,
                    None,
                    r.payment_id,
                    r.ledger_entry_id,
                    None,
                    r.created_at,
                    None,
                    r.created_at,
                    evidence_fingerprint=r.preview_fingerprint,
                    consumption_ledger_entry_id=r.credit_consumption_ledger_entry_id,
                )
            )
    for r in db.execute(
        select(
            PrepaidFundingBaseline.id,
            PrepaidFundingBaseline.account_id,
            PrepaidFundingBaseline.currency,
            PrepaidFundingBaseline.amount,
            PrepaidFundingBaseline.is_active,
            PrepaidFundingBaseline.created_at,
            PrepaidFundingBaseline.superseded_at,
            PrepaidFundingBaseline.position_at,
            PrepaidFundingBaseline.batch_id,
        ).order_by(PrepaidFundingBaseline.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.funding_baseline,
                r.id,
                r.account_id,
                r.currency,
                r.amount,
                None,
                "reviewed_baseline",
                r.is_active,
                None,
                None,
                None,
                None,
                r.created_at,
                r.superseded_at,
                r.position_at,
                source_kind="reconstruction_batch",
                source_id=r.batch_id,
            )
        )
    for r in db.execute(
        select(
            CustomerSubledgerOpeningPosition.id,
            CustomerSubledgerOpeningPosition.account_id,
            CustomerSubledgerOpeningPosition.currency,
            CustomerSubledgerOpeningPosition.legacy_position,
            CustomerSubledgerOpeningPosition.opening_delta,
            CustomerSubledgerOpeningPosition.created_at,
            CustomerSubledgerOpeningPosition.occurred_at,
            CustomerSubledgerOpeningPosition.baseline_id,
            CustomerSubledgerOpeningPosition.evidence_fingerprint,
        ).order_by(CustomerSubledgerOpeningPosition.id)
    ):
        result.append(
            FinancialSourceEvidence(
                SourceKind.opening_position,
                r.id,
                r.account_id,
                r.currency,
                r.legacy_position,
                r.opening_delta,
                "reviewed_opening",
                True,
                None,
                None,
                None,
                None,
                r.created_at,
                None,
                r.occurred_at,
                source_kind="funding_baseline",
                source_id=r.baseline_id,
                evidence_fingerprint=r.evidence_fingerprint,
            )
        )
    return tuple(result)


def _settlements(db: Session) -> tuple[SettlementEvidence, ...]:
    rows: list[SettlementEvidence] = []
    for r in db.execute(
        select(
            PaymentAllocation.id,
            PaymentAllocation.invoice_id,
            PaymentAllocation.payment_id,
            PaymentAllocation.amount,
            PaymentAllocation.ledger_entry_id,
            PaymentAllocation.consumption_ledger_entry_id,
            PaymentAllocation.is_active,
            PaymentAllocation.created_at,
        ).order_by(PaymentAllocation.id)
    ):
        rows.append(SettlementEvidence(SettlementKind.payment, *r))
    for r in db.execute(
        select(
            CreditNoteApplication.id,
            CreditNoteApplication.invoice_id,
            CreditNoteApplication.credit_note_id,
            CreditNoteApplication.amount,
            CreditNoteApplication.ledger_entry_id,
            CreditNoteApplication.consumption_ledger_entry_id,
            CreditNoteApplication.created_at,
        ).order_by(CreditNoteApplication.id)
    ):
        rows.append(
            SettlementEvidence(
                SettlementKind.credit_note,
                r.id,
                r.invoice_id,
                r.credit_note_id,
                r.amount,
                r.ledger_entry_id,
                r.consumption_ledger_entry_id,
                True,
                r.created_at,
            )
        )
    for r in db.execute(
        select(
            PrepaidOpeningFundingConsumption.id,
            PrepaidOpeningFundingConsumption.invoice_id,
            PrepaidOpeningFundingConsumption.opening_position_id,
            PrepaidOpeningFundingConsumption.baseline_id,
            PrepaidOpeningFundingConsumption.amount,
            PrepaidOpeningFundingConsumption.ledger_entry_id,
            PrepaidOpeningFundingConsumption.created_at,
            PrepaidOpeningFundingConsumption.account_id,
            PrepaidOpeningFundingConsumption.currency,
            PrepaidOpeningFundingConsumption.consumed_at,
            PrepaidOpeningFundingConsumption.reconciliation_fingerprint,
        ).order_by(PrepaidOpeningFundingConsumption.id)
    ):
        source_id = r.opening_position_id or r.baseline_id
        # The schema requires one structural source; missing evidence is a capture failure.
        if source_id is None:
            raise _fail(
                "schema_evidence_missing",
                "Opening consumption has no structural source.",
            )
        rows.append(
            SettlementEvidence(
                SettlementKind.opening_funding,
                r.id,
                r.invoice_id,
                source_id,
                r.amount,
                r.ledger_entry_id,
                None,
                True,
                r.created_at,
                r.account_id,
                r.currency,
                r.consumed_at,
                r.reconciliation_fingerprint,
            )
        )
    return tuple(rows)


def _exclusion(invoice: InvoiceOpeningEvidence) -> ExclusionReason | None:
    if not invoice.active or invoice.proforma:
        return ExclusionReason.inactive_or_proforma
    if invoice.status == InvoiceStatus.draft:
        return ExclusionReason.draft
    if invoice.status == InvoiceStatus.void:
        return ExclusionReason.void
    if (
        invoice.status in {InvoiceStatus.paid, InvoiceStatus.written_off}
        or invoice.residual == ZERO
    ):
        return ExclusionReason.fully_closed
    return None


def preview_billing_opening_export(
    db: Session, *, query: PreviewBillingOpeningExportQuery
) -> BillingOpeningExportPreview:
    """Capture every declared source row; classify eligibility separately.

    No date/cohort/subscription join filters may shrink this population. A
    successful capture can contain blockers and is never import authorization.
    """
    # Restore autoflush exactly; never complete or create the caller transaction.
    with db.no_autoflush:
        capture = _capture(db, query)
        subscribers = tuple(db.scalars(select(Subscriber.id).order_by(Subscriber.id)))
        sources = _sources(db)
        settlements = _settlements(db)
        lines = tuple(
            InvoiceLineEvidence(*r)
            for r in db.execute(
                select(
                    InvoiceLine.id,
                    InvoiceLine.invoice_id,
                    InvoiceLine.subscription_id,
                    InvoiceLine.amount,
                    InvoiceLine.quantity,
                    InvoiceLine.unit_price,
                    InvoiceLine.is_active,
                    InvoiceLine.billing_line_key,
                    InvoiceLine.created_at,
                    InvoiceLine.updated_at,
                ).order_by(InvoiceLine.id)
            )
        )
        for source in sources:
            _finite(source.amount, source.secondary_amount)
        for settlement in settlements:
            _finite(settlement.amount)
        for line in lines:
            _finite(line.amount, line.quantity, line.unit_price)
        invoices: list[InvoiceOpeningEvidence] = []
        for r in db.execute(
            select(
                Invoice.id,
                Invoice.account_id,
                Invoice.currency,
                Invoice.invoice_number,
                Invoice.status,
                Invoice.is_active,
                Invoice.is_proforma,
                Invoice.issued_at,
                Invoice.due_at,
                Invoice.due_date_basis,
                Invoice.due_date_basis_ref,
                Invoice.due_date_policy_version,
                Invoice.total,
                Invoice.balance_due,
                Invoice.created_at,
                Invoice.updated_at,
            ).order_by(Invoice.id)
        ):
            _finite(r.total, r.balance_due)
            applied = resolve_invoice_settlement_amounts(db, r.id)
            _finite(
                applied.payments_applied,
                applied.credits_applied,
                applied.opening_funding_applied,
            )
            due_known = (
                r.due_date_basis is not None
                and r.due_date_basis != InvoiceDueDateBasis.unknown_unverified
                and bool(r.due_date_basis_ref)
                and bool(r.due_date_policy_version)
                and r.due_at is not None
            )
            invoices.append(
                InvoiceOpeningEvidence(
                    r.id,
                    r.account_id,
                    r.currency,
                    dict(query.currency_precisions).get(r.currency),
                    r.invoice_number,
                    r.status,
                    r.is_active,
                    r.is_proforma,
                    r.issued_at,
                    r.due_at,
                    r.due_date_basis,
                    r.due_date_basis_ref,
                    r.due_date_policy_version,
                    r.total,
                    r.balance_due,
                    applied.payments_applied,
                    applied.credits_applied,
                    applied.opening_funding_applied,
                    applied.total_applied,
                    r.total - applied.total_applied,
                    r.created_at,
                    r.updated_at,
                    due_known,
                    tuple(l.id for l in lines if l.invoice_id == r.id),
                    tuple(s.id for s in settlements if s.invoice_id == r.id),
                )
            )
        effects = tuple(
            PositionEffectEvidence(
                r.id,
                r.group_id,
                r.effect.value,
                r.amount,
                r.currency,
                r.obligation_id,
                r.invoice_id,
                r.payment_id,
                r.credit_note_id,
                r.entitlement_id,
            )
            for r in db.execute(
                select(
                    CustomerPositionEffect.id,
                    CustomerPositionEffect.group_id,
                    CustomerPositionEffect.effect,
                    CustomerPositionEffect.amount,
                    CustomerPositionEffect.currency,
                    CustomerPositionEffect.obligation_id,
                    CustomerPositionEffect.invoice_id,
                    CustomerPositionEffect.payment_id,
                    CustomerPositionEffect.credit_note_id,
                    CustomerPositionEffect.entitlement_id,
                ).order_by(CustomerPositionEffect.id)
            )
        )
        cutover = authority_cutover(db)
        return _classify(
            db,
            query,
            capture,
            subscribers,
            tuple(invoices),
            lines,
            settlements,
            sources,
            effects,
            cutover.id
            if cutover is not None and _utc(cutover.cutover_at) <= query.economic_cutoff
            else None,
        )


def _classify(
    db: Session,
    query: PreviewBillingOpeningExportQuery,
    capture: CaptureEvidence,
    subscribers: tuple[UUID, ...],
    invoices: tuple[InvoiceOpeningEvidence, ...],
    lines: tuple[InvoiceLineEvidence, ...],
    settlements: tuple[SettlementEvidence, ...],
    sources: tuple[FinancialSourceEvidence, ...],
    effects: tuple[PositionEffectEvidence, ...],
    cutover_id: UUID | None,
) -> BillingOpeningExportPreview:
    for invoice in invoices:
        _finite(
            invoice.total,
            invoice.stored_residual,
            invoice.payments_applied,
            invoice.credits_applied,
            invoice.opening_funding_applied,
            invoice.canonical_discharge,
            invoice.residual,
        )
    for source in sources:
        _finite(source.amount, source.secondary_amount)
    for settlement in settlements:
        _finite(settlement.amount)
    for line in lines:
        _finite(line.amount, line.quantity, line.unit_price)
    for effect in effects:
        _finite(effect.amount)
    exceptions: list[OpeningException] = []
    exclusions: list[OpeningExclusion] = []
    subscriber_set = set(subscribers)
    invoice_map = {i.invoice_id: i for i in invoices}
    source_map = {(s.kind, s.id): s for s in sources}
    ledger_map = {s.id: s for s in sources if s.kind == SourceKind.ledger}
    keys = {(i.account_id, i.currency) for i in invoices} | {
        (s.account_id, s.currency) for s in sources
    }
    cutoff = query.economic_cutoff

    def block(
        code: ExceptionCode,
        account: UUID | None,
        currency: str | None,
        source: UUID,
        detail: str,
    ) -> None:
        exceptions.append(OpeningException(code, account, currency, source, detail))

    def late(*times: datetime | None) -> bool:
        return any(t is not None and _utc(t) > cutoff for t in times)

    for effect in effects:
        group = source_map.get((SourceKind.posting_group, effect.group_id))
        if group is None or group.currency != effect.currency:
            block(
                ExceptionCode.settlement_mapping,
                group.account_id if group else None,
                effect.currency,
                effect.id,
                "Position effect has no same-currency posting group.",
            )
        for kind, linked_id in (
            (SourceKind.payment, effect.payment_id),
            (SourceKind.credit_note, effect.credit_note_id),
        ):
            linked = source_map.get((kind, linked_id)) if linked_id else None
            if linked_id and (
                linked is None
                or group is None
                or (linked.account_id, linked.currency)
                != (group.account_id, group.currency)
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    group.account_id if group else None,
                    effect.currency,
                    effect.id,
                    "Effect structural source missing or crosses account/currency.",
                )
        if effect.invoice_id is not None:
            linked_invoice = invoice_map.get(effect.invoice_id)
            if (
                linked_invoice is None
                or group is None
                or (linked_invoice.account_id, linked_invoice.currency)
                != (group.account_id, group.currency)
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    group.account_id if group else None,
                    effect.currency,
                    effect.id,
                    "Effect invoice missing or crosses account/currency.",
                )

    for source in sources:
        if source.account_id not in subscriber_set:
            block(
                ExceptionCode.account_mapping,
                source.account_id,
                source.currency,
                source.id,
                "No structural subscriber mapping; metadata is never identity.",
            )
        if late(source.created_at, source.updated_at, source.economic_at):
            block(
                ExceptionCode.source_after_cutoff,
                source.account_id,
                source.currency,
                source.id,
                "Current source row crosses cutoff; mutable historical state cannot be reconstructed.",
            )
        precision = dict(query.currency_precisions).get(source.currency)
        if precision != 2 or any(
            a is not None and (not a.is_finite() or a != a.quantize(Decimal("0.01")))
            for a in (source.amount, source.secondary_amount)
        ):
            block(
                ExceptionCode.currency_precision,
                source.account_id,
                source.currency,
                source.id,
                "Source currency precision missing, unsupported or amounts not exact.",
            )
        if source.invoice_id is not None:
            linked = invoice_map.get(source.invoice_id)
            if linked is None or (linked.account_id, linked.currency) != (
                source.account_id,
                source.currency,
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    source.account_id,
                    source.currency,
                    source.id,
                    "Invoice link missing or account/currency mismatch.",
                )
        if (
            source.kind == SourceKind.posting_group
            and source.authority != BillingRecordAuthority.authoritative
        ):
            block(
                ExceptionCode.source_authority,
                source.account_id,
                source.currency,
                source.id,
                "Shadow posting retained only as comparison evidence.",
            )
        if source.kind in {SourceKind.refund, SourceKind.reversal}:
            payment = source_map.get((SourceKind.payment, source.payment_id))
            if payment is None or (payment.account_id, payment.currency) != (
                source.account_id,
                source.currency,
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    source.account_id,
                    source.currency,
                    source.id,
                    "Refund/reversal payment absent or crosses account/currency.",
                )
        if (
            source.kind == SourceKind.credit_note
            and source.active
            and source.amount != source.secondary_amount
            and source.ledger_entry_id not in ledger_map
        ):
            block(
                ExceptionCode.unbacked_lane,
                source.account_id,
                source.currency,
                source.id,
                "Remaining credit note has no exact funding ledger evidence.",
            )

    for line in lines:
        invoice = invoice_map.get(line.invoice_id)
        if invoice is None:
            block(
                ExceptionCode.settlement_mapping,
                None,
                None,
                line.id,
                "Invoice line has no invoice; retained in complete capture.",
            )
        elif late(line.created_at, line.updated_at):
            block(
                ExceptionCode.source_after_cutoff,
                invoice.account_id,
                invoice.currency,
                line.id,
                "Current invoice line crosses cutoff.",
            )

    seen_ids: set[tuple[SettlementKind, UUID]] = set()
    seen_payment_links: set[tuple[UUID, UUID]] = set()
    seen_ledger_links: set[UUID] = set()
    applications_by_ledger = {
        s.ledger_entry_id: s
        for s in settlements
        if s.kind == SettlementKind.credit_note
        and s.amount > ZERO
        and s.ledger_entry_id is not None
    }
    for settlement in settlements:
        invoice = invoice_map.get(settlement.invoice_id)
        account = invoice.account_id if invoice else None
        currency = invoice.currency if invoice else None
        if invoice is None:
            block(
                ExceptionCode.settlement_mapping,
                account,
                currency,
                settlement.id,
                "Settlement has no structural invoice.",
            )
        if settlement.recorded_account_id is not None and (
            settlement.recorded_account_id,
            settlement.recorded_currency,
        ) != (account, currency):
            block(
                ExceptionCode.settlement_mapping,
                account,
                currency,
                settlement.id,
                "Opening consumption recorded account/currency differs from invoice.",
            )
        if late(settlement.created_at, settlement.economic_at):
            block(
                ExceptionCode.source_after_cutoff,
                account,
                currency,
                settlement.id,
                "Settlement recorded after cutoff.",
            )
        if settlement.kind == SettlementKind.opening_funding:
            funding = source_map.get(
                (SourceKind.opening_position, settlement.source_id)
            ) or source_map.get((SourceKind.funding_baseline, settlement.source_id))
            if funding is None or (funding.account_id, funding.currency) != (
                account,
                currency,
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    account,
                    currency,
                    settlement.id,
                    "Opening funding source absent or crosses account/currency.",
                )
        if settlement.kind != SettlementKind.opening_funding:
            kind = (
                SourceKind.payment
                if settlement.kind == SettlementKind.payment
                else SourceKind.credit_note
            )
            source = source_map.get((kind, settlement.source_id))
            if (
                source is None
                or invoice is None
                or (source.account_id, source.currency) != (account, currency)
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    account,
                    currency,
                    settlement.id,
                    "Settlement source absent or crosses account/currency.",
                )
        if settlement.active and (
            settlement.ledger_entry_id is None
            or settlement.ledger_entry_id not in ledger_map
        ):
            block(
                ExceptionCode.settlement_provenance,
                account,
                currency,
                settlement.id,
                "Active settlement has no exact ledger reference.",
            )
        for ledger_id in (
            settlement.ledger_entry_id,
            settlement.consumption_ledger_entry_id,
        ):
            ledger = ledger_map.get(ledger_id) if ledger_id else None
            if ledger_id is not None and (
                ledger is None
                or (ledger.account_id, ledger.currency) != (account, currency)
            ):
                block(
                    ExceptionCode.settlement_mapping,
                    account,
                    currency,
                    settlement.id,
                    "Settlement ledger reference absent or crosses account/currency.",
                )
        identity = (settlement.kind, settlement.id)
        payment_link = (settlement.source_id, settlement.invoice_id)
        if identity in seen_ids or (
            settlement.kind == SettlementKind.payment
            and payment_link in seen_payment_links
        ):
            block(
                ExceptionCode.duplicate_identity,
                account,
                currency,
                settlement.id,
                "Duplicate source row or payment/invoice allocation identity.",
            )
        seen_ids.add(identity)
        if settlement.kind == SettlementKind.payment:
            seen_payment_links.add(payment_link)
        if not settlement.active:
            continue
        for ledger_id, consumption in (
            (settlement.ledger_entry_id, False),
            (settlement.consumption_ledger_entry_id, True),
        ):
            ledger = ledger_map.get(ledger_id) if ledger_id else None
            if ledger is not None and not _settlement_ledger_matches(
                settlement, ledger, consumption=consumption
            ):
                block(
                    ExceptionCode.settlement_provenance,
                    account,
                    currency,
                    settlement.id,
                    "Settlement ledger lacks exact source, document/payment, direction, active state or amount affinity.",
                )
            if ledger_id is not None and ledger_id in seen_ledger_links:
                block(
                    ExceptionCode.duplicate_identity,
                    account,
                    currency,
                    settlement.id,
                    "One ledger row backs more than one settlement movement.",
                )
            if ledger_id is not None:
                seen_ledger_links.add(ledger_id)
        if settlement.amount == ZERO or (
            settlement.amount < ZERO and settlement.kind != SettlementKind.credit_note
        ):
            block(
                ExceptionCode.settlement_provenance,
                account,
                currency,
                settlement.id,
                "Non-credit application settlement must be positive.",
            )
        if settlement.kind == SettlementKind.credit_note and settlement.amount < ZERO:
            reversal_ledger = (
                ledger_map.get(settlement.ledger_entry_id)
                if settlement.ledger_entry_id
                else None
            )
            original = (
                applications_by_ledger.get(reversal_ledger.reverses_id)
                if reversal_ledger
                else None
            )
            if original is None or (
                original.source_id,
                original.invoice_id,
                original.amount,
            ) != (settlement.source_id, settlement.invoice_id, -settlement.amount):
                block(
                    ExceptionCode.settlement_provenance,
                    account,
                    currency,
                    settlement.id,
                    "Negative credit application lacks exact linked original ledger/application evidence.",
                )
            elif (
                original.consumption_ledger_entry_id is None
                and settlement.consumption_ledger_entry_id is not None
            ):
                block(
                    ExceptionCode.settlement_provenance,
                    account,
                    currency,
                    settlement.id,
                    "Credit reversal releases funding that its original application never consumed.",
                )
            elif original.consumption_ledger_entry_id is not None:
                consumption = (
                    ledger_map.get(settlement.consumption_ledger_entry_id)
                    if settlement.consumption_ledger_entry_id
                    else None
                )
                if (
                    consumption is None
                    or consumption.reverses_id != original.consumption_ledger_entry_id
                ):
                    block(
                        ExceptionCode.settlement_provenance,
                        account,
                        currency,
                        settlement.id,
                        "Credit application funding reversal lacks exact original consumption link.",
                    )

    numbers = Counter(i.invoice_number for i in invoices if i.invoice_number)
    line_keys = Counter(
        l.billing_line_key for l in lines if l.active and l.billing_line_key
    )
    for line in lines:
        invoice = invoice_map.get(line.invoice_id)
        if (
            line.billing_line_key
            and line_keys[line.billing_line_key] > 1
            and invoice is not None
        ):
            block(
                ExceptionCode.duplicate_identity,
                invoice.account_id,
                invoice.currency,
                line.id,
                "Duplicate active billing line identity.",
            )
    for invoice in invoices:
        key = (invoice.account_id, invoice.currency)
        if invoice.invoice_number and numbers[invoice.invoice_number] > 1:
            block(
                ExceptionCode.duplicate_identity,
                *key,
                invoice.invoice_id,
                "Duplicate document number retained for review.",
            )
        if invoice.account_id not in subscriber_set:
            block(
                ExceptionCode.account_mapping,
                *key,
                invoice.invoice_id,
                "Invoice has no structural subscriber.",
            )
        if late(invoice.created_at, invoice.updated_at, invoice.issued_at):
            block(
                ExceptionCode.source_after_cutoff,
                *key,
                invoice.invoice_id,
                "Current invoice crosses cutoff; historical invoice values are unavailable.",
            )
        if any(
            not amount.is_finite() or amount != amount.quantize(Decimal("0.01"))
            for amount in (
                invoice.total,
                invoice.stored_residual,
                invoice.canonical_discharge,
                invoice.residual,
            )
        ):
            block(
                ExceptionCode.currency_precision,
                *key,
                invoice.invoice_id,
                "Invoice amounts cannot be represented exactly in source precision.",
            )
        if invoice.precision != 2:
            block(
                ExceptionCode.currency_precision,
                *key,
                invoice.invoice_id,
                "Two-decimal source precision evidence required by canonical settlement owner.",
            )
        if invoice.residual != invoice.stored_residual:
            block(
                ExceptionCode.residual_mismatch,
                *key,
                invoice.invoice_id,
                "Canonical discharge residual differs exactly from stored balance; no tolerance or balancing plug.",
            )
        if invoice.residual < ZERO:
            block(
                ExceptionCode.negative_residual,
                *key,
                invoice.invoice_id,
                "Negative residual retained without clamping.",
            )
        reason = _exclusion(invoice)
        if reason:
            exclusions.append(OpeningExclusion(invoice.invoice_id, reason))
            if invoice.stored_residual != ZERO or (
                reason == ExclusionReason.fully_closed and invoice.residual != ZERO
            ):
                block(
                    ExceptionCode.excluded_nonzero_residue,
                    *key,
                    invoice.invoice_id,
                    "Excluded document retains nonzero residual evidence.",
                )
        elif (
            invoice.issued_at is None
            or not invoice.invoice_number
            or not invoice.line_ids
        ):
            block(
                ExceptionCode.invoice_provenance,
                *key,
                invoice.invoice_id,
                "Open document requires original issue, number and structural line evidence.",
            )

    # Every independent account/currency lane is counted, including keys with
    # no invoice or subscription. No available-credit/funding cash inference.
    lanes: list[LaneEvidence] = []
    for account, currency in sorted(keys, key=lambda k: (str(k[0]), str(k[1]))):
        if account is None:
            continue
        authority = (
            BillingRecordAuthority.authoritative
            if cutover_id
            else BillingRecordAuthority.shadow
        )
        evidence = resolve_position_evidence_at(
            db, account_id=account, currency=currency, authority=authority, as_of=cutoff
        )
        position = evidence.position
        _finite(
            position.collectible_receivable,
            position.unapplied_customer_credit,
            position.prepaid_funding_reserved,
            position.prepaid_funding_consumed,
            position.written_off_total,
            position.refunded_total,
            position.adjustment_total,
        )
        lane = LaneEvidence(
            account,
            currency,
            authority,
            cutover_id,
            evidence.fingerprint,
            evidence.posting_group_count,
            position.collectible_receivable,
            position.unapplied_customer_credit,
            position.prepaid_funding_reserved,
            position.prepaid_funding_consumed,
            position.written_off_total,
            position.refunded_total,
            position.adjustment_total,
        )
        lanes.append(lane)
        if cutover_id is None:
            block(
                ExceptionCode.source_authority,
                account,
                currency,
                account,
                "No effective runtime customer-subledger authority cutover; comparison only.",
            )
        expected_ar = sum(
            (
                i.residual
                for i in invoices
                if (i.account_id, i.currency) == (account, currency)
                and _exclusion(i) is None
            ),
            ZERO,
        )
        if position.collectible_receivable != expected_ar:
            block(
                ExceptionCode.residual_mismatch,
                account,
                currency,
                account,
                "Independent immutable AR lane differs exactly from open invoice residuals.",
            )
        if any(
            value != ZERO
            for value in (
                position.unapplied_customer_credit,
                position.prepaid_funding_reserved,
                position.prepaid_funding_consumed,
                position.written_off_total,
                position.refunded_total,
                position.adjustment_total,
            )
        ):
            block(
                ExceptionCode.unbacked_lane,
                account,
                currency,
                account,
                "Non-AR lane captured separately; no reviewed opening mapping exists in this census.",
            )
        if not evidence.posting_group_count and any(
            s.active
            and s.kind == SourceKind.ledger
            and s.invoice_id is None
            and s.amount != ZERO
            for s in sources
            if (s.account_id, s.currency) == (account, currency)
        ):
            block(
                ExceptionCode.unbacked_lane,
                account,
                currency,
                account,
                "Unallocated ledger evidence has no bounded immutable lane backing.",
            )

    active_accounts = {account for account, _ in keys}
    exclusions.extend(
        OpeningExclusion(s, ExclusionReason.no_financial_activity)
        for s in subscribers
        if s not in active_accounts
    )
    blocked_keys = {(e.account_id, e.currency) for e in exceptions}
    eligible = tuple(
        i.invoice_id
        for i in invoices
        if i.residual > ZERO
        and _exclusion(i) is None
        and (i.account_id, i.currency) not in blocked_keys
    )
    excluded_ids = {e.source_id for e in exclusions}
    control_keys = keys | {(e.account_id, e.currency) for e in exceptions}
    controls: list[OpeningControl] = []
    for account, currency in sorted(control_keys, key=lambda k: (str(k[0]), str(k[1]))):
        cohort = tuple(
            i for i in invoices if (i.account_id, i.currency) == (account, currency)
        )
        controls.append(
            OpeningControl(
                account,
                currency,
                len(cohort),
                sum(i.invoice_id in eligible for i in cohort),
                sum(i.invoice_id in excluded_ids for i in cohort),
                sum(
                    (e.account_id, e.currency) == (account, currency)
                    for e in exceptions
                ),
                sum((i.total for i in cohort), ZERO),
                sum((i.canonical_discharge for i in cohort), ZERO),
                sum((i.residual for i in cohort), ZERO),
                sum((i.residual for i in cohort if i.invoice_id in eligible), ZERO),
                sum((s.account_id, s.currency) == (account, currency) for s in sources),
            )
        )
    result = BillingOpeningExportPreview(
        capture,
        subscribers,
        invoices,
        lines,
        settlements,
        sources,
        tuple(lanes),
        effects,
        eligible,
        tuple(exclusions),
        tuple(exceptions),
        tuple(controls),
        "",
    )
    return replace(result, content_fingerprint=_fingerprint(result))
