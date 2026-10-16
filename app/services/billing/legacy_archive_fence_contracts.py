"""Closed archive component contracts; observations describe uncommitted staging only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType
from uuid import UUID

DatabaseOid = NewType("DatabaseOid", int)
RoleOid = NewType("RoleOid", int)
RelationOid = NewType("RelationOid", int)
FunctionOid = NewType("FunctionOid", int)
TriggerOid = NewType("TriggerOid", int)
TransactionId = NewType("TransactionId", str)
CatalogDigest = NewType("CatalogDigest", str)


class ArchiveFenceRevision(StrEnum):
    V1 = "billing-private-archive-fence-v1"


class ArchiveRelation(StrEnum):
    ACCOUNT_ADJUSTMENTS = "account_adjustments"
    BANK_RECONCILIATION_ITEMS = "bank_reconciliation_items"
    BANK_RECONCILIATION_RUNS = "bank_reconciliation_runs"
    BILLING_ACCOUNT_CREDIT_ALLOCATION_ITEMS = "billing_account_credit_allocation_items"
    BILLING_ACCOUNT_CREDIT_ALLOCATIONS = "billing_account_credit_allocations"
    BILLING_ACCOUNT_LEDGER_ENTRIES = "billing_account_ledger_entries"
    BILLING_RUNS = "billing_runs"
    CONSOLIDATED_CREDIT_CONSUMPTION_RECONCILIATION_EVIDENCE = (
        "consolidated_credit_consumption_reconciliation_evidence"
    )
    CONSOLIDATED_PAYMENT_RETURN_ALLOCATION_EVIDENCE = (
        "consolidated_payment_return_allocation_evidence"
    )
    CONSOLIDATED_PAYMENT_RETURN_DOCUMENT_RECONSTRUCTION_EVIDENCE = (
        "consolidated_payment_return_document_reconstruction_evidence"
    )
    CONSOLIDATED_PAYMENT_RETURN_RECONCILIATION_EVIDENCE = (
        "consolidated_payment_return_reconciliation_evidence"
    )
    CONSOLIDATED_PAYMENT_SETTLEMENT_RECONCILIATION_EVIDENCE = (
        "consolidated_payment_settlement_reconciliation_evidence"
    )
    CREDIT_NOTE_APPLICATIONS = "credit_note_applications"
    CREDIT_NOTE_LINES = "credit_note_lines"
    CREDIT_NOTES = "credit_notes"
    CUSTOMER_POSITION_EFFECTS = "customer_position_effects"
    CUSTOMER_POSTING_GROUPS = "customer_posting_groups"
    CUSTOMER_SUBLEDGER_AUTHORITY_CUTOVERS = "customer_subledger_authority_cutovers"
    CUSTOMER_SUBLEDGER_OPENING_CORRECTIONS = "customer_subledger_opening_corrections"
    CUSTOMER_SUBLEDGER_OPENING_POSITIONS = "customer_subledger_opening_positions"
    CUTOVER_BALANCE_VARIANCES = "cutover_balance_variances"
    INVOICE_CLOSURE_LEDGER_EVIDENCE = "invoice_closure_ledger_evidence"
    INVOICE_CLOSURES = "invoice_closures"
    INVOICE_DISCOUNT_HISTORY = "invoice_discount_history"
    INVOICE_LINES = "invoice_lines"
    INVOICES = "invoices"
    LEDGER_ENTRIES = "ledger_entries"
    NATIVE_PREPAID_OPENING_REPAIRS = "native_prepaid_opening_repairs"
    PAYMENT_ALLOCATION_RECONCILIATION_EXCEPTIONS = (
        "payment_allocation_reconciliation_exceptions"
    )
    PAYMENT_ALLOCATIONS = "payment_allocations"
    PAYMENT_IMPORT_BATCH_REVERSAL_ITEMS = "payment_import_batch_reversal_items"
    PAYMENT_IMPORT_BATCH_REVERSALS = "payment_import_batch_reversals"
    PAYMENT_REFUNDS = "payment_refunds"
    PAYMENT_REVERSALS = "payment_reversals"
    PAYMENT_SETTLEMENTS = "payment_settlements"
    PAYMENTS = "payments"
    PREPAID_ACTIVATION_FUNDING_OVERRIDES = "prepaid_activation_funding_overrides"
    PREPAID_DRAFT_RECONCILIATION_EXCEPTIONS = "prepaid_draft_reconciliation_exceptions"
    PREPAID_FUNDING_BASELINES = "prepaid_funding_baselines"
    PREPAID_FUNDING_RECONSTRUCTION_BATCHES = "prepaid_funding_reconstruction_batches"
    PREPAID_FUNDING_TRIGGER_EXECUTIONS = "prepaid_funding_trigger_executions"
    PREPAID_FUNDING_TRIGGER_SUBSCRIPTION_OUTCOMES = (
        "prepaid_funding_trigger_subscription_outcomes"
    )
    PREPAID_OPENING_FUNDING_CONSUMPTIONS = "prepaid_opening_funding_consumptions"
    SPLYNX_BILLING_TRANSACTIONS = "splynx_billing_transactions"


@dataclass(frozen=True)
class ArchiveFenceRequest:
    revision: ArchiveFenceRevision = ArchiveFenceRevision.V1

    def __post_init__(self) -> None:
        if self.revision is not ArchiveFenceRevision.V1:
            raise ValueError("Unsupported archive fence revision")


@dataclass(frozen=True)
class ArchiveRelationObservation:
    relation: ArchiveRelation
    oid: RelationOid
    row_guard_oid: TriggerOid
    statement_guard_oid: TriggerOid


@dataclass(frozen=True)
class ArchiveFenceStagedObservation:
    """Transaction-local catalog facts, never a receipt or admission proof."""

    revision: ArchiveFenceRevision
    command_id: UUID
    database_oid: DatabaseOid
    backend_pid: int
    transaction_id: TransactionId
    owner_oid: RoleOid
    runtime_oid: RoleOid
    function_oid: FunctionOid
    relations: tuple[ArchiveRelationObservation, ...]
    catalog_digest: CatalogDigest
    replay_observed: bool


@dataclass(frozen=True)
class ArchiveFenceAuditPayload:
    revision: ArchiveFenceRevision
    table_count: int
    catalog_digest: CatalogDigest
    command_id: UUID

    def serialize(self) -> dict[str, object]:
        """Explicit event transport boundary; no mutable domain bags."""
        return {
            "revision": self.revision.value,
            "table_count": self.table_count,
            "catalog_digest": str(self.catalog_digest),
            "command_id": str(self.command_id),
        }
