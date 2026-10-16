"""Complete current-snapshot source census under the invoice domain.

The caller supplies a PostgreSQL READ ONLY, REPEATABLE READ/SERIALIZABLE
transaction. No mutable historical reconstruction or import is performed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import Table, inspect, select, text
from sqlalchemy.orm import Mapper, Session

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
    PaymentProviderEvent,
    PaymentRefund,
    PaymentReversal,
    PaymentSettlement,
    TopupIntent,
)
from app.models.billing_contract import BillingRecordAuthority
from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    CustomerSubledgerOpeningPosition,
)
from app.models.integration_platform import IntegrationInbox
from app.models.payment_proof import PaymentProof, PaymentProofCorrection
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
    BillingOpeningExportPreviewV2,
    BillingOpeningExportPreviewV3,
    BillingOpeningLockInventory,
    BillingOpeningLockInventoryQuery,
    BillingOpeningPopulation,
    CaptureEvidence,
    CaptureLockedBillingOpeningPopulationQuery,
    CashEvidencePopulation,
    CashInboxEvidence,
    CashPaymentEvidence,
    CashProofCorrectionEvidence,
    CashProofEvidence,
    CashProviderEventEvidence,
    CashRefundEvidence,
    CashReversalEvidence,
    CashSettlementEvidence,
    CashSourceCount,
    CashSourceKind,
    CashTopupEvidence,
    CurrentInvoiceDocumentObservation,
    CurrentInvoiceLineTaxObservation,
    DocumentaryObservationPopulation,
    ExceptionCode,
    ExclusionReason,
    FinancialSourceEvidence,
    InvoiceLineEvidence,
    InvoiceOpeningEvidence,
    LaneEvidence,
    LockedBillingOpeningPopulation,
    OpeningControl,
    OpeningException,
    OpeningExclusion,
    OpeningPopulationRelation,
    OriginalEvidenceRequirement,
    OriginalEvidenceUnavailableReason,
    PositionEffectEvidence,
    PreviewBillingOpeningExportQuery,
    SettlementEvidence,
    SettlementKind,
    SourceKind,
    UnavailableOriginalInvoiceEvidence,
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


def _validate_query(query: PreviewBillingOpeningExportQuery) -> None:
    if (
        re.fullmatch(r"[0-9a-f]{40}", query.source_repository_sha) is None
        or query.population_version
        not in {
            "sub-billing-opening-census-v1",
            "sub-billing-opening-census-v2",
            "sub-billing-opening-census-v3",
        }
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


def _capture(db: Session, query: PreviewBillingOpeningExportQuery) -> CaptureEvidence:
    _validate_query(query)
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


# Exact census relations only; this is NOT a complete legacy-writer fence.
# PostgreSQL public-schema relations match the unqualified model selections below.
_PopulationModel = (
    Subscriber
    | Invoice
    | InvoiceLine
    | Payment
    | CreditNote
    | LedgerEntry
    | CustomerPostingGroup
    | CustomerPositionEffect
    | PaymentRefund
    | PaymentReversal
    | PrepaidFundingBaseline
    | CustomerSubledgerOpeningPosition
    | PaymentAllocation
    | CreditNoteApplication
    | PrepaidOpeningFundingConsumption
    | PaymentSettlement
    | PaymentProviderEvent
    | IntegrationInbox
    | PaymentProof
    | PaymentProofCorrection
    | TopupIntent
)
_BASE_POPULATION_MODELS: tuple[type[_PopulationModel], ...] = (
    Subscriber,
    Invoice,
    InvoiceLine,
    Payment,
    CreditNote,
    LedgerEntry,
    CustomerPostingGroup,
    CustomerPositionEffect,
    PaymentRefund,
    PaymentReversal,
    PrepaidFundingBaseline,
    CustomerSubledgerOpeningPosition,
    PaymentAllocation,
    CreditNoteApplication,
    PrepaidOpeningFundingConsumption,
)
_CASH_POPULATION_MODELS: tuple[type[_PopulationModel], ...] = (
    PaymentSettlement,
    PaymentProviderEvent,
    IntegrationInbox,
    PaymentProof,
    PaymentProofCorrection,
    TopupIntent,
)


def _locked_population_relations(db: Session, *, include_cash: bool) -> tuple[str, ...]:
    """Prove the closed ORM inventory uses the lock-check connection unchanged.

    Built-in Session routing is bounded by mapper and Table bindings. Custom
    routing is unsupported; probing examples cannot prove arbitrary get_bind.
    No SQL/population read is issued while resolving these public bindings.
    """
    if (
        getattr(db.get_bind, "__func__", None) is not Session.get_bind
        or getattr(db.connection, "__func__", None) is not Session.connection
    ):
        raise _fail("locked_capture_required", "Custom Session routing is unsupported.")
    bind = db.get_bind()
    connection = db.connection()
    transaction = connection.get_transaction()
    if (
        bind.get_execution_options().get("schema_translate_map")
        or connection.get_execution_options().get("schema_translate_map")
        or transaction is None
        or connection.in_nested_transaction()
    ):
        raise _fail(
            "locked_capture_required",
            "Locked census requires an untranslated active root connection.",
        )
    models = _BASE_POPULATION_MODELS
    if include_cash:
        models += _CASH_POPULATION_MODELS
    relations = ["alembic_version"]
    for model in models:
        mapper = inspect(model)
        if not isinstance(mapper, Mapper):
            raise _fail(
                "locked_capture_required", "Every census model requires a mapped table."
            )
        table = mapper.local_table
        # Check mapper routing AND table-only routing used by Core/helper SQL.
        # Refuse a distinct bind before it can open another connection/transaction.
        if (
            not isinstance(table, Table)
            or table.schema not in {None, "public"}
            or db.get_bind(mapper=mapper) is not bind
            or db.get_bind(clause=table) is not bind
        ):
            raise _fail(
                "locked_capture_required",
                "Every census mapper and table must use the lock-check bind.",
            )
        if (
            db.connection(bind_arguments={"mapper": mapper}) is not connection
            or db.connection(bind_arguments={"clause": table}) is not connection
            or connection.get_transaction() is not transaction
        ):
            raise _fail(
                "locked_capture_required",
                "Every census read must use the lock-check connection and transaction.",
            )
        relations.append(table.name)
    return tuple(relations)


def billing_opening_lock_inventory(
    db: Session, *, query: BillingOpeningLockInventoryQuery
) -> BillingOpeningLockInventory:
    """Expose existing v3 collector tables and prove their single-root routing.

    Reads no census and acquires no relation locks. The caller must qualify its
    root before calling, then lock this inventory before collecting population.
    """
    if not isinstance(query, BillingOpeningLockInventoryQuery):
        raise _fail("invalid_query", "Supply the typed v3 lock inventory query.")
    return BillingOpeningLockInventory(
        tuple(
            OpeningPopulationRelation(name)
            for name in sorted(set(_locked_population_relations(db, include_cash=True)))
        )
    )


def capture_locked_billing_opening_population(
    db: Session, *, query: CaptureLockedBillingOpeningPopulationQuery
) -> LockedBillingOpeningPopulation:
    """Observe the census inside a future handoff owner's locked write transaction.

    Acquires no locks and creates/completes no transaction. Actual PostgreSQL
    state is mandatory; caller declarations never prove a fence or freshness.
    This population carries no eligibility, lane authority or import approval.
    """
    requested = query.source
    _validate_query(requested)
    if (
        not db.in_transaction()
        or db.in_nested_transaction()
        or db.new
        or db.dirty
        or db.deleted
        or db.get_bind().dialect.name != "postgresql"
    ):
        raise _fail(
            "locked_capture_required",
            "Supply a clean active PostgreSQL root transaction.",
        )
    with db.no_autoflush:
        relations = _locked_population_relations(
            db,
            include_cash=requested.population_version
            != "sub-billing-opening-census-v1",
        )
        settings = db.execute(
            text(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            )
        ).one()
        if settings[0] != "read committed" or settings[1] != "off":
            raise _fail(
                "locked_capture_required",
                "Locked capture requires READ WRITE READ COMMITTED; older repeatable snapshots are refused.",
            )
        for relation in relations:
            # No SQL identifier interpolation. Resolve both names to refuse
            # search_path/temp shadowing, and prove this backend's granted lock.
            held = db.execute(
                text("""
                SELECT to_regclass(:relation) = to_regclass(:qualified)
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_inherits
                    WHERE inhrelid = to_regclass(:qualified)
                       OR inhparent = to_regclass(:qualified)
                  )
                  AND (SELECT relkind = 'r' FROM pg_class WHERE oid = to_regclass(:qualified))
                  AND EXISTS (
                    SELECT 1 FROM pg_locks
                    WHERE pid = pg_backend_pid() AND granted
                      AND locktype = 'relation'
                      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                      AND relation = to_regclass(:qualified)
                      AND mode IN ('ShareRowExclusiveLock', 'ExclusiveLock', 'AccessExclusiveLock')
                  )
            """),
                {"relation": relation, "qualified": "public." + relation},
            ).scalar_one()
            if held is not True:
                raise _fail(
                    "population_lock_required",
                    "Every census relation needs a granted current-backend SHARE ROW EXCLUSIVE or stronger lock.",
                )
        # READ COMMITTED obtains a new snapshot after all required locks are
        # granted. All subsequent census reads are stable behind those locks.
        row = db.execute(
            text("SELECT pg_current_snapshot()::text, clock_timestamp()")
        ).one()
        if requested.economic_cutoff > row[1]:
            raise _fail(
                "invalid_query", "Economic cutoff cannot be later than capture."
            )
        heads = tuple(
            db.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars()
        )
        if not heads:
            raise _fail(
                "schema_evidence_missing", "Migration-chain schema heads are required."
            )
        capture = CaptureEvidence(requested, heads, row[0], row[1], settings[0], True)
        population = _population(db, query=requested)
        cash = (
            None
            if requested.population_version == "sub-billing-opening-census-v1"
            else _cash_evidence(db)
        )
        documentary = (
            _documentary_observations(db, invoices=population.invoices)
            if requested.population_version == "sub-billing-opening-census-v3"
            else None
        )
        return LockedBillingOpeningPopulation(
            capture, relations, population, cash, documentary
        )


def _sources(db: Session) -> tuple[FinancialSourceEvidence, ...]:
    result: list[FinancialSourceEvidence] = []
    for payment_row in db.execute(
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
                payment_row.id,
                payment_row.account_id,
                payment_row.currency,
                payment_row.amount,
                payment_row.refunded_amount,
                payment_row.status.value,
                payment_row.is_active,
                None,
                payment_row.id,
                None,
                None,
                payment_row.created_at,
                payment_row.updated_at,
                payment_row.paid_at,
            )
        )
    for credit_note_row in db.execute(
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
                credit_note_row.id,
                credit_note_row.account_id,
                credit_note_row.currency,
                credit_note_row.total,
                credit_note_row.applied_total,
                credit_note_row.status.value,
                credit_note_row.is_active,
                credit_note_row.invoice_id,
                None,
                credit_note_row.funding_ledger_entry_id,
                credit_note_row.void_ledger_entry_id,
                credit_note_row.created_at,
                credit_note_row.updated_at,
                credit_note_row.issued_at,
            )
        )
    for ledger_row in db.execute(
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
                ledger_row.id,
                ledger_row.account_id,
                ledger_row.currency,
                ledger_row.amount,
                None,
                ledger_row.entry_type.value,
                ledger_row.is_active,
                ledger_row.invoice_id,
                ledger_row.payment_id,
                ledger_row.id,
                ledger_row.reversal_of_entry_id,
                ledger_row.created_at,
                ledger_row.updated_at,
                ledger_row.effective_date,
                source_kind=ledger_row.source.value if ledger_row.source else None,
            )
        )
    for group_row in db.execute(
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
                group_row.id,
                group_row.account_id,
                group_row.currency,
                None,
                None,
                group_row.command_kind.value,
                True,
                None,
                None,
                None,
                group_row.reverses_group_id,
                group_row.recorded_at,
                None,
                group_row.occurred_at,
                group_row.authority,
                group_row.source_kind,
                group_row.source_id,
            )
        )
    for model, kind in (
        (PaymentRefund, SourceKind.refund),
        (PaymentReversal, SourceKind.reversal),
    ):
        for adjustment_row in db.execute(
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
                    adjustment_row.id,
                    adjustment_row.account_id,
                    adjustment_row.currency,
                    adjustment_row.amount,
                    None,
                    adjustment_row.origin.value,
                    True,
                    None,
                    adjustment_row.payment_id,
                    adjustment_row.ledger_entry_id,
                    None,
                    adjustment_row.created_at,
                    None,
                    adjustment_row.created_at,
                    evidence_fingerprint=adjustment_row.preview_fingerprint,
                    consumption_ledger_entry_id=adjustment_row.credit_consumption_ledger_entry_id,
                )
            )
    for baseline_row in db.execute(
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
                baseline_row.id,
                baseline_row.account_id,
                baseline_row.currency,
                baseline_row.amount,
                None,
                "reviewed_baseline",
                baseline_row.is_active,
                None,
                None,
                None,
                None,
                baseline_row.created_at,
                baseline_row.superseded_at,
                baseline_row.position_at,
                source_kind="reconstruction_batch",
                source_id=baseline_row.batch_id,
            )
        )
    for opening_row in db.execute(
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
                opening_row.id,
                opening_row.account_id,
                opening_row.currency,
                opening_row.legacy_position,
                opening_row.opening_delta,
                "reviewed_opening",
                True,
                None,
                None,
                None,
                None,
                opening_row.created_at,
                None,
                opening_row.occurred_at,
                source_kind="funding_baseline",
                source_id=opening_row.baseline_id,
                evidence_fingerprint=opening_row.evidence_fingerprint,
            )
        )
    return tuple(result)


def _settlements(db: Session) -> tuple[SettlementEvidence, ...]:
    rows: list[SettlementEvidence] = []
    for payment_allocation_row in db.execute(
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
        rows.append(SettlementEvidence(SettlementKind.payment, *payment_allocation_row))
    for credit_application_row in db.execute(
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
                credit_application_row.id,
                credit_application_row.invoice_id,
                credit_application_row.credit_note_id,
                credit_application_row.amount,
                credit_application_row.ledger_entry_id,
                credit_application_row.consumption_ledger_entry_id,
                True,
                credit_application_row.created_at,
            )
        )
    for opening_consumption_row in db.execute(
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
        source_id = (
            opening_consumption_row.opening_position_id
            or opening_consumption_row.baseline_id
        )
        # The schema requires one structural source; missing evidence is a capture failure.
        if source_id is None:
            raise _fail(
                "schema_evidence_missing",
                "Opening consumption has no structural source.",
            )
        rows.append(
            SettlementEvidence(
                SettlementKind.opening_funding,
                opening_consumption_row.id,
                opening_consumption_row.invoice_id,
                source_id,
                opening_consumption_row.amount,
                opening_consumption_row.ledger_entry_id,
                None,
                True,
                opening_consumption_row.created_at,
                opening_consumption_row.account_id,
                opening_consumption_row.currency,
                opening_consumption_row.consumed_at,
                opening_consumption_row.reconciliation_fingerprint,
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


def _population(
    db: Session, *, query: PreviewBillingOpeningExportQuery
) -> BillingOpeningPopulation:
    """Collect typed observations; qualification remains outside this helper."""
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
    settlement_ids: dict[UUID, list[UUID]] = defaultdict(list)
    line_ids: dict[UUID, list[UUID]] = defaultdict(list)
    for settlement in settlements:
        _finite(settlement.amount)
        settlement_ids[settlement.invoice_id].append(settlement.id)
    for line in lines:
        _finite(line.amount, line.quantity, line.unit_price)
        line_ids[line.invoice_id].append(line.id)
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
                tuple(line_ids.get(r.id, ())),
                tuple(settlement_ids.get(r.id, ())),
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
    return BillingOpeningPopulation(
        subscribers, tuple(invoices), lines, settlements, sources, effects
    )


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
        population = _population(db, query=query)
        subscribers = population.subscriber_ids
        invoices = population.invoices
        lines = population.lines
        settlements = population.settlements
        sources = population.financial_sources
        effects = population.effects
        cutover = authority_cutover(db)
        report = _classify(
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

        if query.population_version == "sub-billing-opening-census-v1":
            return report
        cash = _classify_cash(_cash_evidence(db), query=query, report=report)
        extended = BillingOpeningExportPreviewV2(
            report.capture,
            report.subscriber_ids,
            report.invoices,
            report.lines,
            report.settlements,
            report.financial_sources,
            report.lanes,
            report.effects,
            report.eligible_invoice_ids,
            report.exclusions,
            report.blocking_exceptions,
            report.controls,
            "",
            cash,
        )
        if query.population_version == "sub-billing-opening-census-v2":
            return replace(extended, content_fingerprint=_fingerprint(extended))
        observations = _documentary_observations(db, invoices=report.invoices)
        documentary = BillingOpeningExportPreviewV3(
            report.capture,
            report.subscriber_ids,
            report.invoices,
            report.lines,
            report.settlements,
            report.financial_sources,
            report.lanes,
            report.effects,
            report.eligible_invoice_ids,
            report.exclusions,
            report.blocking_exceptions,
            report.controls,
            "",
            cash,
            observations,
        )
        return replace(documentary, content_fingerprint=_fingerprint(documentary))


def _documentary_observations(
    db: Session, *, invoices: tuple[InvoiceOpeningEvidence, ...]
) -> DocumentaryObservationPopulation:
    """Observe current columns without reconstructing or qualifying originals.

    No TaxRate, party/settings or PDF lookup can supply an issued snapshot.
    Null line snapshots remain null, including lines with no tax-rate identity.
    Existing AR eligibility and cash observations are independent of these gaps.
    """
    invoice_evidence = invoices
    invoices_observed = tuple(
        CurrentInvoiceDocumentObservation(*row)
        for row in db.execute(
            select(
                Invoice.id,
                Invoice.subtotal,
                Invoice.discount_amount,
                Invoice.tax_total,
                Invoice.total,
                Invoice.billing_period_start,
                Invoice.billing_period_end,
            ).order_by(Invoice.id)
        )
    )
    lines = tuple(
        CurrentInvoiceLineTaxObservation(*row)
        for row in db.execute(
            select(
                InvoiceLine.id,
                InvoiceLine.invoice_id,
                InvoiceLine.description,
                InvoiceLine.tax_rate_id,
                InvoiceLine.tax_rate_snapshot_version,
                InvoiceLine.tax_rate_code_snapshot,
                InvoiceLine.tax_rate_percent_snapshot,
                InvoiceLine.tax_rate_is_active_snapshot,
                InvoiceLine.tax_application,
            ).order_by(InvoiceLine.id)
        )
    )
    for invoice in invoices_observed:
        _finite(
            invoice.subtotal, invoice.discount_amount, invoice.tax_total, invoice.total
        )
    for line in lines:
        _finite(line.tax_rate_percent_snapshot)
    return DocumentaryObservationPopulation(
        current_invoices=invoices_observed,
        current_lines=lines,
        unavailable_original_evidence=tuple(
            UnavailableOriginalInvoiceEvidence(
                invoice_id=invoice.invoice_id,
                requirements=tuple(OriginalEvidenceRequirement),
                reasons=tuple(OriginalEvidenceUnavailableReason),
            )
            for invoice in invoice_evidence
        ),
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
    keys: set[tuple[UUID | None, str]] = set()
    invoice_cohorts: dict[
        tuple[UUID | None, str | None], list[InvoiceOpeningEvidence]
    ] = defaultdict(list)
    source_cohorts: dict[
        tuple[UUID | None, str | None], list[FinancialSourceEvidence]
    ] = defaultdict(list)
    for item in invoices:
        _finite(
            item.total,
            item.stored_residual,
            item.payments_applied,
            item.credits_applied,
            item.opening_funding_applied,
            item.canonical_discharge,
            item.residual,
        )
        invoice_cohorts[(item.account_id, item.currency)].append(item)
        keys.add((item.account_id, item.currency))
    for source in sources:
        _finite(source.amount, source.secondary_amount)
        source_cohorts[(source.account_id, source.currency)].append(source)
        keys.add((source.account_id, source.currency))
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
            source_invoice = invoice_map.get(source.invoice_id)
            if source_invoice is None or (
                source_invoice.account_id,
                source_invoice.currency,
            ) != (
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
            payment = (
                source_map.get((SourceKind.payment, source.payment_id))
                if source.payment_id is not None
                else None
            )
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
        line_invoice = invoice_map.get(line.invoice_id)
        if line_invoice is None:
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
                line_invoice.account_id,
                line_invoice.currency,
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
        settlement_invoice = invoice_map.get(settlement.invoice_id)
        account = settlement_invoice.account_id if settlement_invoice else None
        currency = settlement_invoice.currency if settlement_invoice else None
        if settlement_invoice is None:
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
            settlement_source = source_map.get((kind, settlement.source_id))
            if (
                settlement_source is None
                or settlement_invoice is None
                or (settlement_source.account_id, settlement_source.currency)
                != (account, currency)
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
                if reversal_ledger is not None
                and reversal_ledger.reverses_id is not None
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
                consumption_ledger = (
                    ledger_map.get(settlement.consumption_ledger_entry_id)
                    if settlement.consumption_ledger_entry_id
                    else None
                )
                if (
                    consumption_ledger is None
                    or consumption_ledger.reverses_id
                    != original.consumption_ledger_entry_id
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
        duplicate_line_invoice = invoice_map.get(line.invoice_id)
        if (
            line.billing_line_key
            and line_keys[line.billing_line_key] > 1
            and duplicate_line_invoice is not None
        ):
            block(
                ExceptionCode.duplicate_identity,
                duplicate_line_invoice.account_id,
                duplicate_line_invoice.currency,
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
        cohort = invoice_cohorts.get((account, currency), ())
        expected_ar = sum((i.residual for i in cohort if _exclusion(i) is None), ZERO)
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
            for s in source_cohorts.get((account, currency), ())
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
    exception_counts = Counter((e.account_id, e.currency) for e in exceptions)
    eligible = tuple(
        i.invoice_id
        for i in invoices
        if i.residual > ZERO
        and _exclusion(i) is None
        and (i.account_id, i.currency) not in exception_counts
    )
    eligible_ids = set(eligible)
    excluded_ids = {e.source_id for e in exclusions}
    control_keys = keys | exception_counts.keys()
    controls: list[OpeningControl] = []
    for account, currency in sorted(control_keys, key=lambda k: (str(k[0]), str(k[1]))):
        cohort = invoice_cohorts.get((account, currency), ())
        controls.append(
            OpeningControl(
                account,
                currency,
                len(cohort),
                sum(i.invoice_id in eligible_ids for i in cohort),
                sum(i.invoice_id in excluded_ids for i in cohort),
                exception_counts[(account, currency)],
                sum((i.total for i in cohort), ZERO),
                sum((i.canonical_discharge for i in cohort), ZERO),
                sum((i.residual for i in cohort), ZERO),
                sum((i.residual for i in cohort if i.invoice_id in eligible_ids), ZERO),
                len(source_cohorts.get((account, currency), ())),
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


def _cash_evidence(db: Session) -> CashEvidencePopulation:
    """Capture whole raw receipt populations in the existing caller snapshot."""
    payments = tuple(
        CashPaymentEvidence(*row)
        for row in db.execute(
            select(
                Payment.id,
                Payment.account_id,
                Payment.billing_account_id,
                Payment.provider_id,
                Payment.external_id,
                Payment.amount,
                Payment.provider_fee,
                Payment.refunded_amount,
                Payment.currency,
                Payment.status,
                Payment.is_active,
                Payment.paid_at,
                Payment.reserved_for_purchase_id,
                Payment.import_run_id,
                Payment.created_at,
                Payment.updated_at,
            ).order_by(Payment.id)
        )
    )
    payment_settlements = tuple(
        CashSettlementEvidence(*row)
        for row in db.execute(
            select(
                PaymentSettlement.id,
                PaymentSettlement.payment_id,
                PaymentSettlement.origin,
                PaymentSettlement.amount,
                PaymentSettlement.unallocated_amount,
                PaymentSettlement.prepaid_amount,
                PaymentSettlement.currency,
                PaymentSettlement.unallocated_ledger_entry_id,
                PaymentSettlement.prepaid_ledger_entry_id,
                PaymentSettlement.billing_account_ledger_entry_id,
                PaymentSettlement.preview_fingerprint,
                PaymentSettlement.idempotency_key,
                PaymentSettlement.created_at,
            ).order_by(PaymentSettlement.id)
        )
    )
    provider_events = tuple(
        CashProviderEventEvidence(*row)
        for row in db.execute(
            select(
                PaymentProviderEvent.id,
                PaymentProviderEvent.provider_id,
                PaymentProviderEvent.payment_id,
                PaymentProviderEvent.invoice_id,
                PaymentProviderEvent.external_id,
                PaymentProviderEvent.event_type,
                PaymentProviderEvent.idempotency_key,
                PaymentProviderEvent.source,
                PaymentProviderEvent.status,
                PaymentProviderEvent.observed_payment_status,
                PaymentProviderEvent.amount,
                PaymentProviderEvent.provider_fee,
                PaymentProviderEvent.net_amount,
                PaymentProviderEvent.currency,
                PaymentProviderEvent.provider_reference,
                PaymentProviderEvent.financial_effect,
                PaymentProviderEvent.observation_digest,
                PaymentProviderEvent.error_code,
                PaymentProviderEvent.received_at,
                PaymentProviderEvent.processed_at,
            ).order_by(PaymentProviderEvent.id)
        )
    )
    refunds = tuple(
        CashRefundEvidence(*row)
        for row in db.execute(
            select(
                PaymentRefund.id,
                PaymentRefund.payment_id,
                PaymentRefund.provider_event_id,
                PaymentRefund.ledger_entry_id,
                PaymentRefund.billing_account_ledger_entry_id,
                PaymentRefund.credit_consumption_ledger_entry_id,
                PaymentRefund.amount,
                PaymentRefund.currency,
                PaymentRefund.origin,
                PaymentRefund.preview_fingerprint,
                PaymentRefund.created_at,
            ).order_by(PaymentRefund.id)
        )
    )
    reversals = tuple(
        CashReversalEvidence(*row)
        for row in db.execute(
            select(
                PaymentReversal.id,
                PaymentReversal.payment_id,
                PaymentReversal.provider_event_id,
                PaymentReversal.ledger_entry_id,
                PaymentReversal.billing_account_ledger_entry_id,
                PaymentReversal.credit_consumption_ledger_entry_id,
                PaymentReversal.amount,
                PaymentReversal.currency,
                PaymentReversal.origin,
                PaymentReversal.preview_fingerprint,
                PaymentReversal.created_at,
            ).order_by(PaymentReversal.id)
        )
    )
    proofs = tuple(
        CashProofEvidence(*row)
        for row in db.execute(
            select(
                PaymentProof.id,
                PaymentProof.account_id,
                PaymentProof.billing_account_id,
                PaymentProof.payment_id,
                PaymentProof.amount,
                PaymentProof.gross_amount,
                PaymentProof.wht_amount,
                PaymentProof.verified_amount,
                PaymentProof.currency,
                PaymentProof.status,
                PaymentProof.paid_at,
                PaymentProof.created_at,
                PaymentProof.updated_at,
            ).order_by(PaymentProof.id)
        )
    )
    proof_corrections = tuple(
        CashProofCorrectionEvidence(*row)
        for row in db.execute(
            select(
                PaymentProofCorrection.id,
                PaymentProofCorrection.duplicate_proof_id,
                PaymentProofCorrection.original_proof_id,
                PaymentProofCorrection.duplicate_payment_id,
                PaymentProofCorrection.payment_reversal_id,
                PaymentProofCorrection.ledger_entry_id,
                PaymentProofCorrection.preview_fingerprint,
                PaymentProofCorrection.idempotency_key,
                PaymentProofCorrection.created_at,
            ).order_by(PaymentProofCorrection.id)
        )
    )
    topups = tuple(
        CashTopupEvidence(*row)
        for row in db.execute(
            select(
                TopupIntent.id,
                TopupIntent.account_id,
                TopupIntent.billing_account_id,
                TopupIntent.invoice_id,
                TopupIntent.completed_payment_id,
                TopupIntent.provider_id,
                TopupIntent.capability_binding_id,
                TopupIntent.currency,
                TopupIntent.requested_amount,
                TopupIntent.actual_amount,
                TopupIntent.external_id,
                TopupIntent.status,
                TopupIntent.completed_at,
                TopupIntent.created_at,
                TopupIntent.updated_at,
            ).order_by(TopupIntent.id)
        )
    )
    inbox_rows: list[CashInboxEvidence] = []
    for inbox_row in db.execute(
        select(
            IntegrationInbox.id,
            IntegrationInbox.installation_id,
            IntegrationInbox.capability_binding_id,
            IntegrationInbox.provider_event_id,
            IntegrationInbox.event_type,
            IntegrationInbox.payload_digest,
            IntegrationInbox.state,
            IntegrationInbox.consequence_json["provider_event_id"].as_string(),
            IntegrationInbox.consequence_json["payment_id"].as_string(),
            IntegrationInbox.consequence_json["provider_id"].as_string(),
            IntegrationInbox.error_code,
            IntegrationInbox.received_at,
            IntegrationInbox.processed_at,
            IntegrationInbox.created_at,
            IntegrationInbox.updated_at,
        ).order_by(IntegrationInbox.id)
    ):
        event_id, event_valid = _cash_consequence_id(inbox_row[7])
        payment_id, payment_valid = _cash_consequence_id(inbox_row[8])
        provider_id, provider_valid = _cash_consequence_id(inbox_row[9])
        inbox_rows.append(
            CashInboxEvidence(
                id=inbox_row[0],
                installation_id=inbox_row[1],
                capability_binding_id=inbox_row[2],
                provider_event_id=inbox_row[3],
                event_type=inbox_row[4],
                payload_digest=inbox_row[5],
                state=inbox_row[6],
                consequence_provider_event_id=event_id,
                consequence_payment_id=payment_id,
                consequence_provider_id=provider_id,
                consequence_identifiers_valid=event_valid
                and payment_valid
                and provider_valid,
                error_code=inbox_row[10],
                received_at=inbox_row[11],
                processed_at=inbox_row[12],
                created_at=inbox_row[13],
                updated_at=inbox_row[14],
            )
        )
    population = CashEvidencePopulation(
        payments,
        payment_settlements,
        provider_events,
        tuple(inbox_rows),
        refunds,
        reversals,
        proofs,
        proof_corrections,
        topups,
        (
            CashSourceCount(CashSourceKind.payments, len(payments)),
            CashSourceCount(
                CashSourceKind.payment_settlements, len(payment_settlements)
            ),
            CashSourceCount(
                CashSourceKind.payment_provider_events, len(provider_events)
            ),
            CashSourceCount(CashSourceKind.integration_inbox, len(inbox_rows)),
            CashSourceCount(CashSourceKind.payment_refunds, len(refunds)),
            CashSourceCount(CashSourceKind.payment_reversals, len(reversals)),
            CashSourceCount(CashSourceKind.payment_proofs, len(proofs)),
            CashSourceCount(
                CashSourceKind.payment_proof_corrections, len(proof_corrections)
            ),
            CashSourceCount(CashSourceKind.topup_intents, len(topups)),
        ),
        (),
    )
    return population


def _cash_consequence_id(value: object) -> tuple[UUID | None, bool]:
    """Only the known consequence ID scalars are selected; no raw JSON export."""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    try:
        return UUID(value), True
    except ValueError:
        return None, False


def _classify_cash(
    cash: CashEvidencePopulation,
    *,
    query: PreviewBillingOpeningExportQuery,
    report: BillingOpeningExportPreview,
) -> CashEvidencePopulation:
    """Record structural gaps without qualifying a single external cash source."""
    exceptions: list[OpeningException] = []
    payment_map = {p.id: p for p in cash.payments}
    event_map = {e.id: e for e in cash.provider_events}
    proof_map = {p.id: p for p in cash.proofs}
    reversal_map = {r.id: r for r in cash.reversals}
    ledger_map = {
        s.id: s for s in report.financial_sources if s.kind == SourceKind.ledger
    }
    event_counts = Counter(
        e.payment_id for e in cash.provider_events if e.payment_id is not None
    )
    subscribers = set(report.subscriber_ids)

    def block(
        code: ExceptionCode, identifier: UUID, payment_id: UUID | None, detail: str
    ) -> None:
        payment = payment_map.get(payment_id) if payment_id is not None else None
        exceptions.append(
            OpeningException(
                code,
                payment.account_id if payment else None,
                payment.currency if payment else None,
                identifier,
                detail,
            )
        )

    def observe(
        identifier: UUID,
        payment_id: UUID | None,
        times: tuple[datetime | None, ...],
        amounts: tuple[Decimal | None, ...] = (),
    ) -> None:
        _finite(*amounts)
        if any(
            t is not None and (t.tzinfo is None or t.utcoffset() is None) for t in times
        ):
            raise _fail(
                "schema_evidence_missing", "Cash evidence timestamps must be aware."
            )
        if any(t is not None and t > query.economic_cutoff for t in times):
            block(
                ExceptionCode.source_after_cutoff,
                identifier,
                payment_id,
                "Current cash evidence crosses cutoff; no historical reconstruction.",
            )
        block(
            ExceptionCode.cash_unqualified,
            identifier,
            payment_id,
            "Raw receipt evidence has no trusted external-cash attestation or live namespace mapping.",
        )

    def payment_link(
        identifier: UUID, payment_id: UUID | None, currency: str | None
    ) -> None:
        payment = payment_map.get(payment_id) if payment_id is not None else None
        if (
            payment is None
            or payment.account_id not in subscribers
            or (currency is not None and payment.currency != currency)
        ):
            block(
                ExceptionCode.settlement_mapping,
                identifier,
                payment_id,
                "Cash source has no exact same-currency subscriber/payment mapping.",
            )

    def ledger_link(identifier: UUID, payment_id: UUID, ledger_id: UUID | None) -> None:
        if ledger_id is None:
            return
        ledger = ledger_map.get(ledger_id)
        payment = payment_map.get(payment_id)
        if (
            ledger is None
            or payment is None
            or (ledger.payment_id, ledger.account_id, ledger.currency)
            != (payment_id, payment.account_id, payment.currency)
        ):
            block(
                ExceptionCode.settlement_provenance,
                identifier,
                payment_id,
                "Cash ledger reference is absent or crosses payment/account/currency.",
            )

    for cash_payment in cash.payments:
        observe(
            cash_payment.id,
            cash_payment.id,
            (cash_payment.created_at, cash_payment.updated_at, cash_payment.paid_at),
            (
                cash_payment.amount,
                cash_payment.provider_fee,
                cash_payment.refunded_amount,
            ),
        )
        payment_link(cash_payment.id, cash_payment.id, cash_payment.currency)
        if event_counts[cash_payment.id] != 1:
            block(
                ExceptionCode.cash_event_ambiguity,
                cash_payment.id,
                cash_payment.id,
                "Payment has zero or multiple provider observations; no event is selected as proof.",
            )
        if (
            cash_payment.billing_account_id is not None
            or cash_payment.reserved_for_purchase_id is not None
            or cash_payment.import_run_id is not None
        ):
            block(
                ExceptionCode.unbacked_lane,
                cash_payment.id,
                cash_payment.id,
                "Billing-account, purchase reservation or imported cash requires separate reviewed consumption/provenance mapping.",
            )
    for settlement in cash.payment_settlements:
        observe(
            settlement.id,
            settlement.payment_id,
            (settlement.created_at,),
            (
                settlement.amount,
                settlement.unallocated_amount,
                settlement.prepaid_amount,
            ),
        )
        payment_link(settlement.id, settlement.payment_id, settlement.currency)
        ledger_link(
            settlement.id, settlement.payment_id, settlement.unallocated_ledger_entry_id
        )
        ledger_link(
            settlement.id, settlement.payment_id, settlement.prepaid_ledger_entry_id
        )
        if settlement.billing_account_ledger_entry_id is not None:
            block(
                ExceptionCode.unbacked_lane,
                settlement.id,
                settlement.payment_id,
                "Billing-account ledger namespace is retained without qualification.",
            )
    for event in cash.provider_events:
        observe(
            event.id,
            event.payment_id,
            (event.received_at, event.processed_at),
            (event.amount, event.provider_fee, event.net_amount),
        )
        payment_link(event.id, event.payment_id, event.currency)
        event_payment = (
            payment_map.get(event.payment_id) if event.payment_id is not None else None
        )
        if event_payment is not None and event_payment.provider_id != event.provider_id:
            block(
                ExceptionCode.settlement_mapping,
                event.id,
                event.payment_id,
                "Provider observation and payment provider identities differ.",
            )
    for receipt in cash.inbox_receipts:
        observe(
            receipt.id,
            receipt.consequence_payment_id,
            (
                receipt.received_at,
                receipt.processed_at,
                receipt.created_at,
                receipt.updated_at,
            ),
        )
        payment_link(receipt.id, receipt.consequence_payment_id, None)
        linked_event = (
            event_map.get(receipt.consequence_provider_event_id)
            if receipt.consequence_provider_event_id is not None
            else None
        )
        if (
            not receipt.consequence_identifiers_valid
            or linked_event is None
            or linked_event.payment_id != receipt.consequence_payment_id
            or (
                receipt.consequence_provider_id is not None
                and linked_event.provider_id != receipt.consequence_provider_id
            )
        ):
            block(
                ExceptionCode.settlement_mapping,
                receipt.id,
                receipt.consequence_payment_id,
                "Inbox consequence IDs are invalid, absent or disagree with exact event/payment/provider links.",
            )
    movement_records: tuple[CashRefundEvidence | CashReversalEvidence, ...] = (
        *cash.refunds,
        *cash.reversals,
    )
    for movement in movement_records:
        observe(
            movement.id, movement.payment_id, (movement.created_at,), (movement.amount,)
        )
        payment_link(movement.id, movement.payment_id, movement.currency)
        for ledger_id in (
            movement.ledger_entry_id,
            movement.credit_consumption_ledger_entry_id,
        ):
            ledger_link(movement.id, movement.payment_id, ledger_id)
        linked_event = (
            event_map.get(movement.provider_event_id)
            if movement.provider_event_id is not None
            else None
        )
        if movement.provider_event_id is not None and (
            linked_event is None or linked_event.payment_id != movement.payment_id
        ):
            block(
                ExceptionCode.settlement_mapping,
                movement.id,
                movement.payment_id,
                "Refund/reversal provider observation is absent or belongs to another payment.",
            )
    for proof in cash.proofs:
        observe(
            proof.id,
            proof.payment_id,
            (proof.created_at, proof.updated_at, proof.paid_at),
            (proof.amount, proof.gross_amount, proof.wht_amount, proof.verified_amount),
        )
        payment_link(proof.id, proof.payment_id, proof.currency)
        proof_payment = (
            payment_map.get(proof.payment_id) if proof.payment_id is not None else None
        )
        if proof_payment is not None and (
            proof.account_id,
            proof.billing_account_id,
        ) != (
            proof_payment.account_id,
            proof_payment.billing_account_id,
        ):
            block(
                ExceptionCode.settlement_mapping,
                proof.id,
                proof.payment_id,
                "Proof and payment structural account identities differ.",
            )
    for correction in cash.proof_corrections:
        observe(
            correction.id, correction.duplicate_payment_id, (correction.created_at,)
        )
        payment_link(correction.id, correction.duplicate_payment_id, None)
        ledger_link(
            correction.id, correction.duplicate_payment_id, correction.ledger_entry_id
        )
        duplicate = proof_map.get(correction.duplicate_proof_id)
        reversal = reversal_map.get(correction.payment_reversal_id)
        if (
            duplicate is None
            or duplicate.payment_id != correction.duplicate_payment_id
            or correction.original_proof_id not in proof_map
            or reversal is None
            or reversal.payment_id != correction.duplicate_payment_id
        ):
            block(
                ExceptionCode.settlement_mapping,
                correction.id,
                correction.duplicate_payment_id,
                "Proof correction lacks exact proof/payment/reversal structural links.",
            )
    for topup in cash.topups:
        observe(
            topup.id,
            topup.completed_payment_id,
            (topup.created_at, topup.updated_at, topup.completed_at),
            (topup.requested_amount, topup.actual_amount),
        )
        payment_link(topup.id, topup.completed_payment_id, topup.currency)
        topup_payment = (
            payment_map.get(topup.completed_payment_id)
            if topup.completed_payment_id is not None
            else None
        )
        if topup_payment is not None and (
            topup.account_id,
            topup.billing_account_id,
            topup.provider_id,
        ) != (
            topup_payment.account_id,
            topup_payment.billing_account_id,
            topup_payment.provider_id,
        ):
            block(
                ExceptionCode.settlement_mapping,
                topup.id,
                topup.completed_payment_id,
                "Topup and completed payment structural account/provider identities differ.",
            )
    return replace(
        cash, blocking_exceptions=tuple(exceptions), qualified_payment_ids=()
    )
