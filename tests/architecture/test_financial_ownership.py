"""Architecture guardrails for financial write ownership.

Routes and tasks are already guarded, but service-layer adapters can otherwise
write money or lifecycle state directly.  These allowlists capture existing
debt; they are not authorization for new writers and should only shrink.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

LEDGER_ENTRY_CONSTRUCTION = re.compile(r"\bLedgerEntry\s*\(")
CREDIT_NOTE_CONSTRUCTION = re.compile(r"\bCreditNote\s*\(")
CREDIT_NOTE_LINE_CONSTRUCTION = re.compile(r"\bCreditNoteLine\s*\(")
PAYMENT_ALLOCATION_CONSTRUCTION = re.compile(r"\bPaymentAllocation\s*\(")
VAS_WALLET_ENTRY_CONSTRUCTION = re.compile(r"\bVasWalletEntry\s*\(")
INVOICE_STATUS_WRITE = re.compile(r"\.status\s*(?<![=!<>])=(?!=)\s*InvoiceStatus\.")
CREDIT_NOTE_STATUS_WRITE = re.compile(
    r"\.status\s*(?<![=!<>])=(?!=)\s*CreditNoteStatus\."
)

APPROVED_LEDGER_WRITERS = {
    Path("app/services/billing/credit_notes.py"),
    Path("app/services/billing/invoices.py"),
    Path("app/services/billing/ledger.py"),
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/reconcile_unposted.py"),
    # Existing debt outside the canonical billing package.
    Path("app/services/catalog/subscriptions.py"),
    Path("app/services/customer_portal_flow_addons.py"),
    Path("app/services/cutover_balance_audit.py"),
}

APPROVED_CREDIT_NOTE_WRITERS = {Path("app/services/billing/credit_notes.py")}
APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/credit_notes.py"),
}

APPROVED_ALLOCATION_WRITERS = {
    Path("app/services/billing/payments.py"),
    # Existing provider-settlement debt.
    Path("app/services/billing/providers.py"),
}

APPROVED_VAS_WALLET_WRITERS = {Path("app/services/vas_wallet.py")}

APPROVED_INVOICE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/invoices.py"),
    Path("app/services/billing/reconcile_unposted.py"),
    Path("app/services/billing_automation.py"),
    Path("app/services/billing_cleanup_remediation.py"),
    Path("app/services/billing_prepaid_overlap_repair.py"),
    Path("app/services/collections/_core.py"),
    Path("app/services/locking.py"),
    Path("app/services/web_billing_invoice_bulk.py"),
}


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in APP_DIR.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _violations(pattern: re.Pattern[str], approved: set[Path]) -> list[str]:
    found: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved or rel.parts[:2] == ("app", "models"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{rel}:{line}")
    return found


def test_only_approved_modules_post_to_the_ledger() -> None:
    violations = _violations(LEDGER_ENTRY_CONSTRUCTION, APPROVED_LEDGER_WRITERS)
    assert not violations, (
        "LedgerEntry constructed outside an approved money owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_payment_owner_allocates_payments() -> None:
    violations = _violations(
        PAYMENT_ALLOCATION_CONSTRUCTION, APPROVED_ALLOCATION_WRITERS
    )
    assert not violations, (
        "PaymentAllocation constructed outside an approved payment owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_credit_note_owner_creates_credit_notes() -> None:
    violations = _violations(CREDIT_NOTE_CONSTRUCTION, APPROVED_CREDIT_NOTE_WRITERS)
    violations.extend(
        _violations(CREDIT_NOTE_LINE_CONSTRUCTION, APPROVED_CREDIT_NOTE_WRITERS)
    )
    assert not violations, (
        "CreditNote constructed outside the credit-note owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_credit_note_owner_transitions_status() -> None:
    violations = _violations(
        CREDIT_NOTE_STATUS_WRITE, APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS
    )
    assert not violations, (
        "Credit-note status assigned outside the credit-note owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_wallet_owner_writes_wallet_entries() -> None:
    violations = _violations(VAS_WALLET_ENTRY_CONSTRUCTION, APPROVED_VAS_WALLET_WRITERS)
    assert not violations, (
        "VasWalletEntry constructed outside the wallet owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_approved_modules_transition_invoice_status() -> None:
    violations = _violations(INVOICE_STATUS_WRITE, APPROVED_INVOICE_LIFECYCLE_WRITERS)
    assert not violations, (
        "Invoice status assigned outside an approved lifecycle owner:\n  "
        + "\n  ".join(violations)
    )


def test_financial_writer_allowlists_do_not_contain_missing_paths() -> None:
    missing = sorted(
        str(rel)
        for allowlist in (
            APPROVED_LEDGER_WRITERS,
            APPROVED_CREDIT_NOTE_WRITERS,
            APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS,
            APPROVED_ALLOCATION_WRITERS,
            APPROVED_VAS_WALLET_WRITERS,
            APPROVED_INVOICE_LIFECYCLE_WRITERS,
        )
        for rel in allowlist
        if not (PROJECT_ROOT / rel).exists()
    )
    assert not missing, (
        "stale financial-writer allowlist entries must be removed:\n  "
        + "\n  ".join(missing)
    )
