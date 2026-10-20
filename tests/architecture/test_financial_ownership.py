"""Architecture guardrails for financial writes and paid-status business reads.

Routes and tasks are already guarded, but service-layer adapters can otherwise
write money or lifecycle state directly.  These allowlists capture existing
debt; they are not authorization for new writers and should only shrink.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal, TypedDict

import pytest

from tests.architecture.source_index import (
    call_lines,
    identifier_names,
    python_ast,
    python_files,
    python_nodes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
PAID_READ_BASELINE = Path(__file__).with_name("invoice_paid_read_baseline.json")

# Frozen initial census: never expand this ceiling. Retirement updates only the
# reviewed active-state digest, keeping the original predicate identity intact.
PAID_READ_CEILING_SHA256 = (
    "5ae5603a2b6e0b30df1329d674f275244802dfa5d784d2bb134c8ff8ab38f34a"
)
PAID_READ_APPROVED_SHA256 = (
    "a3ca5fe754e950f064ab8fa61d0a163b1369cd53679423ca9e6d73727dcb6d97"
)

APPROVED_LEDGER_WRITERS = {
    Path("app/services/billing/ledger.py"),
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
    # Existing debt outside the canonical billing package.
    Path("app/services/cutover_balance_audit.py"),
}

APPROVED_ACCOUNT_ADJUSTMENT_WRITERS = {Path("app/services/billing/adjustments.py")}

APPROVED_ALLOCATION_WRITERS = {
    Path("app/services/billing/payments.py"),
}

APPROVED_PAYMENT_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_SETTLEMENT_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS = {
    Path("app/services/billing/payments.py")
}

APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}


APPROVED_CREDIT_APPLICATION_WRITERS = {Path("app/services/billing/credit_notes.py")}

APPROVED_CREDIT_NOTE_WRITERS = {Path("app/services/billing/credit_notes.py")}

APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/credit_notes.py"),
}

APPROVED_PAYMENT_REFUND_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_REVERSAL_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_SETTLEMENT_RECONCILIATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_CREDIT_RECONCILIATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS = {
    Path("app/services/financial_import_batch_reversals.py")
}

APPROVED_PAYMENT_LIFECYCLE_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_INVOICE_CLOSURE_WRITERS = {Path("app/services/billing/invoices.py")}

APPROVED_INVOICE_DOCUMENT_WRITERS = {Path("app/services/billing/invoices.py")}

APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS = {
    Path("app/services/collections/_core.py")
}

APPROVED_PAYMENT_ARRANGEMENT_WRITERS = {Path("app/services/payment_arrangements.py")}

APPROVED_INVOICE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/invoices.py"),
    # Slice 6a (ADR 0031): the Starter issuance lane mirrors the Starter
    # document's issued status/number onto the Sub row; Starter billing is
    # the ledger authority and this module writes no Sub ledger state.
    Path("app/services/billing/starter_issuance.py"),
    # Settlement back-half (ADR 0031): mirrors paid/partially-paid status
    # and balance from the Starter collectible position after a Starter
    # allocation; writes no Sub ledger state.
    Path("app/services/billing/starter_settlement.py"),
    # Credits lane (ADR 0031): mirrors paid status and balance from the
    # Starter collectible position after a Starter credit issuance;
    # writes no Sub ledger state.
    Path("app/services/billing/starter_credit_notes.py"),
    # Prepaid lane (ADR 0031): mirrors paid/partially-paid status and
    # balance from the Starter collectible position after an earmarked
    # Starter allocation; writes no Sub ledger state.
    Path("app/services/billing/starter_prepaid.py"),
}


def _python_files() -> list[Path]:
    return list(python_files(APP_DIR))


def _constructor_lines(path: Path, class_name: str) -> list[int]:
    return list(call_lines(path).get(class_name, ()))


@cache
def _enum_status_write_lines(path: Path, enum_name: str) -> list[int]:
    lines: list[int] = []
    for node in python_nodes(path):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == enum_name
        ):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


def _invoice_status_write_lines(path: Path) -> list[int]:
    return _enum_status_write_lines(path, "InvoiceStatus")


@cache
def _billing_account_balance_write_lines(path: Path) -> list[int]:
    """Find balance assignments in modules that depend on BillingAccount."""
    if "BillingAccount" not in identifier_names(path):
        return []
    lines: list[int] = []
    for node in python_nodes(path):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "balance"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


@cache
def _invoice_terminal_status_write_lines(path: Path) -> list[int]:
    lines: list[int] = []
    for node in python_nodes(path):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "InvoiceStatus"
            and value.attr in {"void", "written_off"}
        ):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


def _violations(
    finder,
    approved: set[Path],
) -> list[str]:
    found: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved or rel.parts[:2] == ("app", "models"):
            continue
        for line in finder(path):
            found.append(f"{rel}:{line}")
    return found


def test_only_approved_modules_post_to_the_ledger() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "LedgerEntry"),
        APPROVED_LEDGER_WRITERS,
    )
    assert not violations, (
        "LedgerEntry constructed outside an approved money owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_account_adjustment_owner_constructs_adjustment_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "AccountAdjustment"),
        APPROVED_ACCOUNT_ADJUSTMENT_WRITERS,
    )
    assert not violations, (
        "AccountAdjustment constructed outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_the_payment_owner_allocates_payments() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "PaymentAllocation"),
        APPROVED_ALLOCATION_WRITERS,
    )
    assert not violations, (
        "PaymentAllocation constructed outside an approved payment owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_payment_owner_creates_payment_documents_and_settlements() -> None:
    payments = _violations(
        lambda path: _constructor_lines(path, "Payment"),
        APPROVED_PAYMENT_WRITERS,
    )
    settlements = _violations(
        lambda path: _constructor_lines(path, "PaymentSettlement"),
        APPROVED_PAYMENT_SETTLEMENT_WRITERS,
    )
    assert not payments, "Payment constructed outside its owner:\n  " + "\n  ".join(
        payments
    )
    assert not settlements, (
        "PaymentSettlement constructed outside its owner:\n  "
        + "\n  ".join(settlements)
    )


def test_only_payment_owner_constructs_allocation_reconciliation_exceptions() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "PaymentAllocationReconciliationException"
        ),
        APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS,
    )
    assert not violations, (
        "PaymentAllocationReconciliationException constructed outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_consolidated_payment_owner_writes_billing_account_ledger() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "BillingAccountLedgerEntry"),
        APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS,
    )
    assert not violations, (
        "BillingAccountLedgerEntry constructed outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_consolidated_credit_owner_constructs_allocation_evidence() -> None:
    decisions = _violations(
        lambda path: _constructor_lines(path, "BillingAccountCreditAllocation"),
        APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
    )
    items = _violations(
        lambda path: _constructor_lines(path, "BillingAccountCreditAllocationItem"),
        APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
    )
    assert not decisions, (
        "BillingAccountCreditAllocation constructed outside its owner:\n  "
        + "\n  ".join(decisions)
    )
    assert not items, (
        "BillingAccountCreditAllocationItem constructed outside its owner:\n  "
        + "\n  ".join(items)
    )


def test_only_consolidated_owner_constructs_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentSettlementReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_SETTLEMENT_RECONCILIATION_WRITERS,
    )
    assert not violations, (
        "Consolidated settlement reconciliation evidence constructed outside "
        "its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_credit_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedCreditConsumptionReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_CREDIT_RECONCILIATION_WRITERS,
    )
    assert not violations, (
        "Consolidated credit-consumption reconciliation evidence constructed "
        "outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_return_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    assert not violations, (
        "Consolidated return reconciliation evidence constructed outside "
        "its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_return_document_reconstruction() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnDocumentReconstructionEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    assert not violations, (
        "Consolidated return-document reconstruction evidence constructed "
        "outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_money_owner_writes_billing_account_balance() -> None:
    violations = _violations(
        _billing_account_balance_write_lines,
        APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS,
    )
    assert not violations, (
        "BillingAccount.balance written outside its ledger-backed owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_invoice_owner_constructs_terminal_closure_evidence() -> None:
    closures = _violations(
        lambda path: _constructor_lines(path, "InvoiceClosure"),
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    ledger_links = _violations(
        lambda path: _constructor_lines(path, "InvoiceClosureLedgerEvidence"),
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    transitions = _violations(
        _invoice_terminal_status_write_lines,
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    assert not closures, (
        "InvoiceClosure constructed outside its owner:\n  " + "\n  ".join(closures)
    )
    assert not ledger_links, (
        "Invoice closure ledger evidence constructed outside its owner:\n  "
        + "\n  ".join(ledger_links)
    )
    assert not transitions, (
        "Invoice void/write-off status transitioned outside its owner:\n  "
        + "\n  ".join(transitions)
    )


def test_only_the_invoice_owner_constructs_invoice_documents_and_lines() -> None:
    invoices = _violations(
        lambda path: _constructor_lines(path, "Invoice"),
        APPROVED_INVOICE_DOCUMENT_WRITERS,
    )
    lines = _violations(
        lambda path: _constructor_lines(path, "InvoiceLine"),
        APPROVED_INVOICE_DOCUMENT_WRITERS,
    )
    assert not invoices, "Invoice constructed outside its owner:\n  " + "\n  ".join(
        invoices
    )
    assert not lines, "InvoiceLine constructed outside its owner:\n  " + "\n  ".join(
        lines
    )


def test_only_collections_owner_constructs_financial_access_evidence() -> None:
    consequences = _violations(
        lambda path: _constructor_lines(path, "FinancialAccessConsequence"),
        APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS,
    )
    evidence = _violations(
        lambda path: _constructor_lines(path, "FinancialAccessConsequenceEvidence"),
        APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS,
    )
    assert not consequences, (
        "Financial access consequence constructed outside collections owner:\n  "
        + "\n  ".join(consequences)
    )
    assert not evidence, (
        "Financial access evidence constructed outside collections owner:\n  "
        + "\n  ".join(evidence)
    )


def test_only_the_credit_note_owner_writes_credit_applications() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "CreditNoteApplication"),
        APPROVED_CREDIT_APPLICATION_WRITERS,
    )
    assert not violations, (
        "CreditNoteApplication constructed outside the credit-note owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_credit_note_owner_constructs_credit_documents_and_lines() -> None:
    documents = _violations(
        lambda path: _constructor_lines(path, "CreditNote"),
        APPROVED_CREDIT_NOTE_WRITERS,
    )
    lines = _violations(
        lambda path: _constructor_lines(path, "CreditNoteLine"),
        APPROVED_CREDIT_NOTE_WRITERS,
    )
    assert not documents, "CreditNote constructed outside its owner:\n  " + "\n  ".join(
        documents
    )
    assert not lines, "CreditNoteLine constructed outside its owner:\n  " + "\n  ".join(
        lines
    )


def test_only_the_credit_note_owner_transitions_credit_status() -> None:
    violations = _violations(
        lambda path: _enum_status_write_lines(path, "CreditNoteStatus"),
        APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS,
    )
    assert not violations, (
        "CreditNote status transitioned outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_arrangement_owner_writes_payment_arrangements() -> None:
    constructors = _violations(
        lambda path: _constructor_lines(path, "PaymentArrangement"),
        APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
    )
    transitions = _violations(
        lambda path: _enum_status_write_lines(path, "ArrangementStatus"),
        APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
    )
    assert not constructors, (
        "PaymentArrangement constructed outside its owner:\n  "
        + "\n  ".join(constructors)
    )
    assert not transitions, (
        "PaymentArrangement status transitioned outside its owner:\n  "
        + "\n  ".join(transitions)
    )


def test_only_the_payment_owner_writes_refunds_reversals_and_payment_status() -> None:
    refunds = _violations(
        lambda path: _constructor_lines(path, "PaymentRefund"),
        APPROVED_PAYMENT_REFUND_WRITERS,
    )
    reversals = _violations(
        lambda path: _constructor_lines(path, "PaymentReversal"),
        APPROVED_PAYMENT_REVERSAL_WRITERS,
    )
    consolidated_evidence = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnAllocationEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    transitions = _violations(
        lambda path: _enum_status_write_lines(path, "PaymentStatus"),
        APPROVED_PAYMENT_LIFECYCLE_WRITERS,
    )
    assert not refunds, (
        "PaymentRefund constructed outside its owner:\n  " + "\n  ".join(refunds)
    )
    assert not reversals, (
        "PaymentReversal constructed outside its owner:\n  " + "\n  ".join(reversals)
    )
    assert not consolidated_evidence, (
        "Consolidated return evidence constructed outside its owner:\n  "
        + "\n  ".join(consolidated_evidence)
    )
    assert not transitions, (
        "Payment status transitioned outside its owner:\n  " + "\n  ".join(transitions)
    )


def test_only_the_import_batch_owner_constructs_batch_reversal_evidence() -> None:
    batches = _violations(
        lambda path: _constructor_lines(path, "PaymentImportBatchReversal"),
        APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
    )
    items = _violations(
        lambda path: _constructor_lines(path, "PaymentImportBatchReversalItem"),
        APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
    )
    assert not batches, (
        "PaymentImportBatchReversal constructed outside its owner:\n  "
        + "\n  ".join(batches)
    )
    assert not items, (
        "PaymentImportBatchReversalItem constructed outside its owner:\n  "
        + "\n  ".join(items)
    )


def test_only_approved_modules_transition_invoice_status() -> None:
    violations = _violations(
        _invoice_status_write_lines,
        APPROVED_INVOICE_LIFECYCLE_WRITERS,
    )
    assert not violations, (
        "Invoice status assigned outside an approved lifecycle owner:\n  "
        + "\n  ".join(violations)
    )


def test_financial_writer_allowlists_only_name_real_writers() -> None:
    operations = (
        (
            APPROVED_LEDGER_WRITERS,
            lambda path: _constructor_lines(path, "LedgerEntry"),
        ),
        (
            APPROVED_ACCOUNT_ADJUSTMENT_WRITERS,
            lambda path: _constructor_lines(path, "AccountAdjustment"),
        ),
        (
            APPROVED_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "PaymentAllocation"),
        ),
        (
            APPROVED_PAYMENT_WRITERS,
            lambda path: _constructor_lines(path, "Payment"),
        ),
        (
            APPROVED_PAYMENT_SETTLEMENT_WRITERS,
            lambda path: _constructor_lines(path, "PaymentSettlement"),
        ),
        (
            APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS,
            lambda path: _constructor_lines(
                path, "PaymentAllocationReconciliationException"
            ),
        ),
        (
            APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountLedgerEntry"),
        ),
        (
            APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountCreditAllocation"),
        ),
        (
            APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountCreditAllocationItem"),
        ),
        (
            APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS,
            _billing_account_balance_write_lines,
        ),
        (
            APPROVED_CREDIT_APPLICATION_WRITERS,
            lambda path: _constructor_lines(path, "CreditNoteApplication"),
        ),
        (
            APPROVED_CREDIT_NOTE_WRITERS,
            lambda path: _constructor_lines(path, "CreditNote"),
        ),
        (
            APPROVED_CREDIT_NOTE_WRITERS,
            lambda path: _constructor_lines(path, "CreditNoteLine"),
        ),
        (
            APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS,
            lambda path: _enum_status_write_lines(path, "CreditNoteStatus"),
        ),
        (
            APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
            lambda path: _constructor_lines(path, "PaymentArrangement"),
        ),
        (
            APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
            lambda path: _enum_status_write_lines(path, "ArrangementStatus"),
        ),
        (
            APPROVED_PAYMENT_REFUND_WRITERS,
            lambda path: _constructor_lines(path, "PaymentRefund"),
        ),
        (
            APPROVED_PAYMENT_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentReversal"),
        ),
        (
            APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentImportBatchReversal"),
        ),
        (
            APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentImportBatchReversalItem"),
        ),
        (
            APPROVED_PAYMENT_LIFECYCLE_WRITERS,
            lambda path: _enum_status_write_lines(path, "PaymentStatus"),
        ),
        (APPROVED_INVOICE_LIFECYCLE_WRITERS, _invoice_status_write_lines),
    )
    stale = sorted(
        str(rel)
        for allowlist, finder in operations
        for rel in allowlist
        if not (PROJECT_ROOT / rel).exists() or not finder(PROJECT_ROOT / rel)
    )
    assert not stale, (
        "stale financial-writer allowlist entries must be removed:\n  "
        + "\n  ".join(stale)
    )


@dataclass(frozen=True, order=True)
class PaidInvoiceRead:
    """Line-independent identity; duplicate predicates are counted separately."""

    path: str
    function: str
    kind: Literal["comparison", "sql_filter", "sql_text"]
    predicate: str


class _PaidInvoiceReads(ast.NodeVisitor):
    """Bounded lexical analysis, not Python/SQLAlchemy type inference.

    Resolves import renames, local/module value aliases, enum collections,
    Invoice annotations, getattr(status), and SQLAlchemy aliased(Invoice).
    Explicit InvoiceStatus operands also identify row-unpacked status reads.
    No directory, filename, or writer-owner exemption applies to reads.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.bindings: dict[str, ast.expr] = {}
        self.invoice_names: set[str] = set()
        self.receiver_models: dict[str, Literal["invoice", "other"]] = {}
        self.reads: Counter[PaidInvoiceRead] = Counter()

    def _resolve(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> ast.expr:
        if isinstance(node, ast.Name) and node.id in self.bindings:
            if node.id not in seen:
                return self._resolve(self.bindings[node.id], seen | {node.id})
            return node
        # Copy expression children without mutating the shared source index.
        result = copy.copy(node)
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.expr):
                setattr(result, field, self._resolve(value, seen))
            elif isinstance(value, list):
                setattr(
                    result,
                    field,
                    [
                        self._resolve(item, seen)
                        if isinstance(item, ast.expr)
                        else item
                        for item in value
                    ],
                )
        return result

    @staticmethod
    def _paid_value(node: ast.AST, *, enum_only: bool) -> bool:
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(
                _PaidInvoiceReads._paid_value(item, enum_only=enum_only)
                for item in node.elts
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "InvoiceStatus"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "paid"
        ):
            return True
        if isinstance(node, ast.Attribute):
            if node.attr == "value":
                return _PaidInvoiceReads._paid_value(node.value, enum_only=enum_only)
            return node.attr == "paid" and (
                isinstance(node.value, ast.Name)
                and node.value.id == "InvoiceStatus"
                or isinstance(node.value, ast.Attribute)
                and node.value.attr == "InvoiceStatus"
            )
        return not enum_only and isinstance(node, ast.Constant) and node.value == "paid"

    def _invoice_status(self, node: ast.AST) -> bool:
        for item in ast.walk(node):
            receiver: ast.expr | None = None
            if isinstance(item, ast.Attribute) and item.attr == "status":
                receiver = item.value
            elif (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "getattr"
                and len(item.args) >= 2
                and isinstance(item.args[1], ast.Constant)
                and item.args[1].value == "status"
            ):
                receiver = item.args[0]
            if receiver is not None:
                if isinstance(receiver, ast.Name) and (
                    receiver.id == "Invoice"
                    or receiver.id in self.invoice_names
                    and self.receiver_models.get(receiver.id) != "other"
                ):
                    return True
                if isinstance(receiver, ast.Attribute) and receiver.attr in {
                    "invoice",
                    "Invoice",
                }:
                    return True
        return False

    def _record(
        self, node: ast.expr, kind: Literal["comparison", "sql_filter"]
    ) -> None:
        resolved = self._resolve(node)
        operands = (
            [resolved.left, *resolved.comparators]
            if isinstance(resolved, ast.Compare)
            else resolved.args
            if isinstance(resolved, ast.Call)
            else []
        )
        enum_paid = any(self._paid_value(item, enum_only=True) for item in operands)
        string_paid = any(self._paid_value(item, enum_only=False) for item in operands)
        if enum_paid or string_paid and self._invoice_status(resolved):
            self.reads[
                PaidInvoiceRead(
                    self.path,
                    ".".join(self.scope) or "<module>",
                    kind,
                    ast.unparse(resolved),
                )
            ] += 1

    @staticmethod
    def _annotation_model(node: ast.expr) -> Literal["invoice", "other"] | None:
        names = {
            item.id if isinstance(item, ast.Name) else item.attr
            for item in ast.walk(node)
            if isinstance(item, (ast.Name, ast.Attribute))
        }
        if "Invoice" in names:
            return "invoice"
        if names & {
            "Payment",
            "PaymentStatus",
            "ExpenseRequest",
            "VendorPurchaseInvoice",
        }:
            return "other"
        return None

    def _query_model(self, node: ast.expr) -> Literal["invoice", "other"] | None:
        node = self._resolve(node)
        if isinstance(node, ast.Name):
            return self.receiver_models.get(node.id)
        if not isinstance(node, ast.Call):
            return None
        method = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else "")
        )
        if method in {"query", "select"} and node.args:
            model = node.args[0]
            if isinstance(model, ast.Name):
                return "invoice" if model.id == "Invoice" else "other"
            if isinstance(model, ast.Attribute) and model.attr == "Invoice":
                return "invoice"
            return None
        if method in {"scalars", "scalar", "execute"} and node.args:
            return self._query_model(node.args[0])
        if isinstance(node.func, ast.Attribute):
            return self._query_model(node.func.value)
        return None

    def _iteration_target(self, target: ast.expr, iterable: ast.expr) -> None:
        model = self._query_model(iterable)
        targets = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
        for item in targets:
            if not isinstance(item, ast.Name):
                continue
            self.bindings.pop(item.id, None)
            self.receiver_models.pop(item.id, None)
            if model is not None and isinstance(target, ast.Name):
                self.receiver_models[item.id] = model
            if (
                model == "invoice"
                and isinstance(target, ast.Name)
                or (model != "other" and "invoice" in item.id.lower())
            ):
                self.invoice_names.add(item.id)
            else:
                self.invoice_names.discard(item.id)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._iteration_target(node.target, node.iter)
        for child in [*node.body, *node.orelse]:
            self.visit(child)

    visit_AsyncFor = visit_For

    def _comprehension_scope(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        previous = (
            self.bindings.copy(),
            self.invoice_names.copy(),
            self.receiver_models.copy(),
        )
        for generator in node.generators:
            self.visit(generator.iter)
            self._iteration_target(generator.target, generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.bindings, self.invoice_names, self.receiver_models = previous

    visit_ListComp = _comprehension_scope
    visit_SetComp = _comprehension_scope
    visit_GeneratorExp = _comprehension_scope
    visit_DictComp = _comprehension_scope

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in {"Invoice", "InvoiceStatus"} and alias.asname:
                self.bindings[alias.asname] = ast.Name(id=alias.name, ctx=ast.Load())

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                value = self._resolve(node.value)
                model = self._query_model(value)
                if model is not None:
                    self.receiver_models[target.id] = model
                    if model == "invoice":
                        self.invoice_names.add(target.id)
                if isinstance(
                    value,
                    (
                        ast.Name,
                        ast.Attribute,
                        ast.Constant,
                        ast.Tuple,
                        ast.List,
                        ast.Set,
                    ),
                ):
                    self.bindings[target.id] = value
                else:
                    self.bindings.pop(target.id, None)
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    if value.func.id == "aliased" and any(
                        isinstance(arg, ast.Name) and arg.id == "Invoice"
                        for arg in value.args
                    ):
                        self.invoice_names.add(target.id)
                # Existing ORM locals frequently lack annotations. Names alone
                # only identify invoice receivers, never generic obj.status.
                if "invoice" in target.id.lower():
                    self.invoice_names.add(target.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            model = self._annotation_model(self._resolve(node.annotation))
            if model is not None:
                self.receiver_models[node.target.id] = model
            if model == "invoice":
                self.invoice_names.add(node.target.id)
            if node.value is not None:
                value = self._resolve(node.value)
                if isinstance(
                    value,
                    (
                        ast.Name,
                        ast.Attribute,
                        ast.Constant,
                        ast.Tuple,
                        ast.List,
                        ast.Set,
                    ),
                ):
                    self.bindings[node.target.id] = value
        if node.value is not None:
            self.visit(node.value)

    def _scope(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        previous = (
            self.bindings.copy(),
            self.invoice_names.copy(),
            self.receiver_models.copy(),
        )
        self.scope.append(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for arg in args:
                self.bindings.pop(arg.arg, None)
                model = (
                    self._annotation_model(self._resolve(arg.annotation))
                    if arg.annotation is not None
                    else None
                )
                self.receiver_models.pop(arg.arg, None)
                if model is not None:
                    self.receiver_models[arg.arg] = model
                if (
                    model == "invoice"
                    or model != "other"
                    and "invoice" in arg.arg.lower()
                ):
                    self.invoice_names.add(arg.arg)
                else:
                    self.invoice_names.discard(arg.arg)
        for child in node.body:
            self.visit(child)
        self.scope.pop()
        self.bindings, self.invoice_names, self.receiver_models = previous

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope
    visit_ClassDef = _scope

    def visit_Compare(self, node: ast.Compare) -> None:
        self._record(node, "comparison")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "in_",
            "notin_",
            "not_in",
            "is_",
            "is_not",
            "__eq__",
            "__ne__",
        }:
            self._record(node, "sql_filter")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "filter_by":
            model = self._query_model(node.func.value)
            for keyword in node.keywords:
                if keyword.arg != "status":
                    continue
                value = self._resolve(keyword.value)
                if (
                    model == "invoice"
                    and self._paid_value(value, enum_only=False)
                    or (model is None and self._paid_value(value, enum_only=True))
                ):
                    # Normalize only this typed keyword predicate, not a query's
                    # unrelated joins/options; its exact occurrence still counts.
                    predicate = ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="Invoice", ctx=ast.Load()),
                            attr="filter_by",
                            ctx=ast.Load(),
                        ),
                        args=[],
                        keywords=[ast.keyword(arg="status", value=value)],
                    )
                    self.reads[
                        PaidInvoiceRead(
                            self.path,
                            ".".join(self.scope) or "<module>",
                            "sql_filter",
                            ast.unparse(predicate),
                        )
                    ] += 1
        # Literal SQL is deliberately limited to a statement that names the
        # invoice table and a paid status condition. It is not a SQL parser.
        if isinstance(node.func, ast.Name) and node.func.id == "text":
            for arg in node.args:
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                sql = " ".join(arg.value.split())
                if re.search(r"\b(?:FROM|JOIN)\s+invoices\b", sql, re.I) and re.search(
                    r"\bstatus\s*(?:=|!=|<>|(?:NOT\s+)?IN\s*\()[^;]*?'paid'", sql, re.I
                ):
                    self.reads[
                        PaidInvoiceRead(
                            self.path,
                            ".".join(self.scope) or "<module>",
                            "sql_text",
                            sql,
                        )
                    ] += 1
        self.generic_visit(node)


def _paid_invoice_reads(tree: ast.Module, path: str) -> Counter[PaidInvoiceRead]:
    visitor = _PaidInvoiceReads(path)
    visitor.visit(tree)
    return visitor.reads


def _paid_read_ratchet_errors(
    current: Counter[PaidInvoiceRead], baseline: Counter[PaidInvoiceRead]
) -> list[str]:
    errors: list[str] = []
    for read in sorted(current.keys() | baseline.keys()):
        if current[read] != baseline[read]:
            action = (
                "new/increased paid read"
                if current[read] > baseline[read]
                else (
                    "retired paid read: lower remaining_count/mark retired in the same change"
                )
            )
            errors.append(f"{action}: {read}: {baseline[read]} -> {current[read]}")
    return errors


class PaidReadBaselineEntry(TypedDict):
    path: str
    function: str
    kind: Literal["comparison", "sql_filter", "sql_text"]
    predicate: str
    count: int
    remaining_count: int
    state: Literal["active", "retired"]
    classification: Literal["business_debt", "report_projection"]
    owner: str
    reason: str


def _paid_baseline_digest(
    entries: list[PaidReadBaselineEntry], *, ceiling: bool
) -> str:
    records = [
        {
            key: value
            for key, value in entry.items()
            if not ceiling or key not in {"state", "remaining_count"}
        }
        for entry in entries
    ]
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _paid_baseline_errors(
    entries: list[PaidReadBaselineEntry], *, ceiling_digest: str, approved_digest: str
) -> list[str]:
    errors: list[str] = []
    if _paid_baseline_digest(entries, ceiling=True) != ceiling_digest:
        errors.append(
            "paid-read inaugural ceiling changed: no new identity/count/classification allowance"
        )
    if _paid_baseline_digest(entries, ceiling=False) != approved_digest:
        errors.append(
            "paid-read active/retired inventory changed without reviewed digest update"
        )
    seen: set[PaidInvoiceRead] = set()
    for entry in entries:
        read = PaidInvoiceRead(
            entry["path"], entry["function"], entry["kind"], entry["predicate"]
        )
        if read in seen:
            errors.append(f"duplicate paid-read baseline identity: {read}")
        seen.add(read)
        if (
            type(entry["count"]) is not int
            or entry["count"] <= 0
            or type(entry["remaining_count"]) is not int
            or not 0 <= entry["remaining_count"] <= entry["count"]
            or entry["state"] != ("active" if entry["remaining_count"] else "retired")
            or entry["kind"] not in {"comparison", "sql_filter", "sql_text"}
            or entry["classification"] not in {"business_debt", "report_projection"}
            or not entry["owner"]
            or not entry["reason"]
        ):
            errors.append(f"invalid paid-read baseline entry: {read}")
    return errors


def test_invoice_paid_business_read_debt_only_shrinks() -> None:
    """All app predicates are measured; allowances are exact corpus entries."""
    entries: list[PaidReadBaselineEntry] = json.loads(
        PAID_READ_BASELINE.read_text(encoding="utf-8")
    )
    assert not (
        errors := _paid_baseline_errors(
            entries,
            ceiling_digest=PAID_READ_CEILING_SHA256,
            approved_digest=PAID_READ_APPROVED_SHA256,
        )
    ), "\n".join(errors)
    baseline: Counter[PaidInvoiceRead] = Counter(
        {
            PaidInvoiceRead(
                entry["path"], entry["function"], entry["kind"], entry["predicate"]
            ): entry["remaining_count"]
            for entry in entries
            if entry["state"] == "active"
        }
    )
    current: Counter[PaidInvoiceRead] = Counter()
    for path in _python_files():
        current.update(
            _paid_invoice_reads(
                python_ast(path), path.relative_to(PROJECT_ROOT).as_posix()
            )
        )
    assert not (errors := _paid_read_ratchet_errors(current, baseline)), "\n".join(
        errors
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "invoice.status == InvoiceStatus.paid",
        "InvoiceStatus.paid is invoice.status",
        "invoice.status != 'paid'",
        "invoice.status == InvoiceStatus('paid')",
        "invoice.status == InvoiceStatus.paid.value",
        "invoice.status.value in {'paid', 'void'}",
        "getattr(invoice, 'status', None) == InvoiceStatus.paid",
        "Invoice.status.in_([InvoiceStatus.paid, InvoiceStatus.void])",
        "Invoice.status.is_('paid')",
        "I.status == S.paid",
        "state == paid",
        "state in paid_states",
        "row.status == 'paid'",
        "paid_invoice.status.in_(['paid'])",
    ],
)
def test_paid_read_detector_rejects_new_business_predicates(predicate: str) -> None:
    source = f"""from app.models.billing import Invoice as I, InvoiceStatus as S
from app.models.billing import Invoice, InvoiceStatus
paid = S.paid
paid_states = (S.paid, S.void)
paid_invoice = aliased(Invoice)
def new_business_gate(invoice: Invoice, row: I):
    state = invoice.status
    return {predicate}
"""
    reads = _paid_invoice_reads(ast.parse(source), "app/services/new_gate.py")
    assert sum(reads.values()) == 1
    assert _paid_read_ratchet_errors(reads, Counter())


def test_paid_read_detector_distinguishes_payment_and_nonpredicates() -> None:
    source = """from app.models.billing import Invoice, InvoiceStatus, Payment
paid = InvoiceStatus.paid
invoice.status = InvoiceStatus.paid
caption = "Invoice.status == InvoiceStatus.paid"
def read(payment: Payment, request: ExpenseRequest):
    return payment.status == 'paid' or request.status in {'approved', 'paid'}
"""
    assert not _paid_invoice_reads(ast.parse(source), "app/services/example.py")
    nested = "def query():\n    return select(Invoice.id).where(Invoice.status == InvoiceStatus.paid) is not None\n"
    assert (
        sum(_paid_invoice_reads(ast.parse(nested), "app/services/example.py").values())
        == 1
    )


def test_paid_read_identity_survives_lines_and_refuses_growth_or_return() -> None:
    source = (
        "def gate(invoice: Invoice):\n    return invoice.status == InvoiceStatus.paid\n"
    )
    baseline = _paid_invoice_reads(ast.parse(source), "app/services/gate.py")
    moved = _paid_invoice_reads(
        ast.parse("\n\n# moved\n" + source), "app/services/gate.py"
    )
    assert not _paid_read_ratchet_errors(moved, baseline)
    assert _paid_read_ratchet_errors(baseline + baseline, baseline)
    assert _paid_read_ratchet_errors(Counter(), baseline)  # stale debt must retire
    retired: Counter[PaidInvoiceRead] = Counter()
    assert _paid_read_ratchet_errors(baseline, retired)  # cannot return after removal


def test_paid_report_allowance_cannot_license_another_business_gate() -> None:
    source = (
        "def get_overview_stats():\n    return Invoice.status == InvoiceStatus.paid\n"
    )
    path = "app/services/billing/reporting.py"
    report = _paid_invoice_reads(ast.parse(source), path)
    assert not _paid_read_ratchet_errors(report, report)
    added = (
        source
        + "def permit_service():\n    return Invoice.status == InvoiceStatus.paid\n"
    )
    assert _paid_read_ratchet_errors(
        _paid_invoice_reads(ast.parse(added), path), report
    )


def test_paid_read_detector_catches_literal_invoice_sql() -> None:
    source = """def gate():
    return text("SELECT id FROM invoices i WHERE i.status IN ('issued', 'paid')")
"""
    reads = _paid_invoice_reads(ast.parse(source), "app/services/raw_gate.py")
    assert sum(reads.values()) == 1
    assert next(iter(reads)).kind == "sql_text"


@pytest.mark.parametrize(
    "field,value",
    [
        ("predicate", "invoice.status == 'paid'"),
        ("count", 2),
        ("classification", "report_projection"),
        ("state", "retired"),
    ],
)
def test_paid_read_baseline_cannot_expand_or_reclassify(
    field: str, value: str | int
) -> None:
    entries: list[PaidReadBaselineEntry] = [
        {
            "path": "app/services/gate.py",
            "function": "gate",
            "kind": "comparison",
            "predicate": "invoice.status == InvoiceStatus.paid",
            "count": 1,
            "remaining_count": 1,
            "state": "active",
            "classification": "business_debt",
            "owner": "financial.invoices",
            "reason": "Existing business gate.",
        }
    ]
    ceiling = _paid_baseline_digest(entries, ceiling=True)
    approved = _paid_baseline_digest(entries, ceiling=False)
    changed = json.loads(json.dumps(entries))
    changed[0][field] = value
    assert _paid_baseline_errors(
        changed, ceiling_digest=ceiling, approved_digest=approved
    )


def test_retired_paid_read_baseline_entry_cannot_be_reactivated() -> None:
    entries: list[PaidReadBaselineEntry] = [
        {
            "path": "app/services/gate.py",
            "function": "gate",
            "kind": "comparison",
            "predicate": "invoice.status == InvoiceStatus.paid",
            "count": 1,
            "remaining_count": 0,
            "state": "retired",
            "classification": "business_debt",
            "owner": "financial.invoices",
            "reason": "Retired business gate.",
        }
    ]
    ceiling = _paid_baseline_digest(entries, ceiling=True)
    approved = _paid_baseline_digest(entries, ceiling=False)
    assert not _paid_baseline_errors(
        entries, ceiling_digest=ceiling, approved_digest=approved
    )
    changed = json.loads(json.dumps(entries))
    changed[0].update(state="active", remaining_count=1)
    assert _paid_baseline_errors(
        changed, ceiling_digest=ceiling, approved_digest=approved
    )


@pytest.mark.parametrize(
    "query",
    [
        "db.query(Invoice).filter_by(status=InvoiceStatus.paid)",
        "db.query(Invoice).filter_by(status='paid')",
        "db.query(I).filter_by(status=S.paid)",
        "select(Invoice).filter_by(status='paid')",
        "invoice_query.filter_by(status='paid')",
    ],
)
def test_paid_read_detector_catches_invoice_filter_by(query: str) -> None:
    source = f"""from app.models.billing import Invoice as I, InvoiceStatus as S
from app.models.billing import Invoice, InvoiceStatus
def gate(db):
    invoice_query = db.query(Invoice).filter(Invoice.is_active.is_(True))
    return {query}
"""
    reads = _paid_invoice_reads(ast.parse(source), "app/services/keyword_gate.py")
    assert sum(reads.values()) == 1
    assert _paid_read_ratchet_errors(reads, Counter())


@pytest.mark.parametrize(
    "body",
    [
        "for invoice in invoices:\n        if invoice.status == 'paid':\n            return True",
        "return [invoice for invoice in invoices if invoice.status == 'paid']",
        "return {invoice.id for invoice in invoices if invoice.status == 'paid'}",
        "return any(invoice.status == 'paid' for invoice in invoices)",
        "return {invoice.id: invoice.status == 'paid' for invoice in invoices}",
        "for row in typed_rows:\n        if row.status == 'paid':\n            return True",
        "return [row for row in db.query(Invoice).all() if row.status == 'paid']",
    ],
)
def test_paid_read_detector_catches_iteration_receivers(body: str) -> None:
    source = f"def gate(invoices, typed_rows: list[Invoice]):\n    {body}\n"
    reads = _paid_invoice_reads(ast.parse(source), "app/services/iteration_gate.py")
    assert sum(reads.values()) == 1
    assert _paid_read_ratchet_errors(reads, Counter())


def test_paid_read_detector_excludes_payment_queries_and_typed_iterations() -> None:
    source = """from app.models.billing import Payment, InvoiceStatus
caption = InvoiceStatus.paid
def gate(db, invoices: list[Payment], invoice: Payment):
    query = db.query(Payment)
    query.filter_by(status='paid')
    db.query(Payment).filter_by(status='paid')
    if invoice.status == 'paid':
        return True
    for invoice in invoices:
        if invoice.status == 'paid':
            return True
    for invoice in db.query(Payment).all():
        if invoice.status == 'paid':
            return True
    return [invoice for invoice in invoices if invoice.status == 'paid']
"""
    assert not _paid_invoice_reads(ast.parse(source), "app/services/payment_read.py")


def test_paid_read_iteration_rebinding_clears_previous_payment_model() -> None:
    source = """def gate(db, invoices):
    for invoice in db.query(Payment).all():
        pass
    for invoice in invoices:
        if invoice.status == 'paid':
            return True
"""
    reads = _paid_invoice_reads(ast.parse(source), "app/services/rebound_gate.py")
    assert sum(reads.values()) == 1
    assert _paid_read_ratchet_errors(reads, Counter())
