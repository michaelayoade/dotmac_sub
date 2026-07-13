"""Architecture guardrails for money ownership.

`tests/architecture/test_thin_wrappers.py` forbids direct queries in `app/web` and
`app/api`, and `test_thin_financial_tasks.py` forbids model imports in four Celery
files. Both hold. But nothing policed the layer BETWEEN them — the ~150
`app/services/web_*.py` modules that sit between the routes and the owners — which
made the thin-wrapper rule trivially evadable: move the write one module down and
the check goes green.

That is exactly where the money strays were found. `web_billing_payments.py`
hand-wrote `PaymentAllocation` rows (no ledger entry, no cap, no recalculation);
`web_billing_invoice_bulk.py` assigns `InvoiceStatus` directly, skipping the
legal-transition guard and the `invoice_sent` event.

This ports the allowlist pattern that has held the ONT boundary
(`test_network_ownership.py`) to money. The allowlists below are TODAY'S DEBT, not
an endorsement. They may only ever shrink. Adding a new file to one is a decision
to be argued for in review, not a formality.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

LEDGER_ENTRY_CONSTRUCTION = re.compile(r"\bLedgerEntry\s*\(")
PAYMENT_ALLOCATION_CONSTRUCTION = re.compile(r"\bPaymentAllocation\s*\(")
VAS_WALLET_ENTRY_CONSTRUCTION = re.compile(r"\bVasWalletEntry\s*\(")
INVOICE_STATUS_WRITE = re.compile(r"\.status\s*(?<![=!<>])=(?!=)\s*InvoiceStatus\.")

# --- LedgerEntry -------------------------------------------------------------
# The ledger is the record of money moving. `financial.ledger` is the declared
# owner, but it is reachable only from REST: every real posting site constructs
# the model by hand. Making the ledger a genuine owner is a bigger slice; until
# then, this at least stops the list growing.
APPROVED_LEDGER_WRITERS = {
    Path("app/services/billing/ledger.py"),
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/invoices.py"),
    Path("app/services/billing/credit_notes.py"),
    Path("app/services/billing/reconcile_unposted.py"),
    # DEBT. These three post money from OUTSIDE the billing package, straight into
    # the enforcement balance, with no linkage validation and no reversal link.
    # They are the reason `financial.ledger` owns nothing.
    Path("app/services/catalog/subscriptions.py"),
    Path("app/services/customer_portal_flow_addons.py"),
    Path("app/services/cutover_balance_audit.py"),
}

# --- PaymentAllocation -------------------------------------------------------
# An allocation and the ledger credit that justifies it must move together. That
# invariant is stated in payments.py and was violated by a hand-written
# allocation in the web layer.
APPROVED_ALLOCATION_WRITERS = {
    Path("app/services/billing/payments.py"),
    # DEBT. providers.py builds an allocation by hand on the non-succeeded branch,
    # uncapped — a NGN50,000 provider payment against a NGN10,000 invoice allocates
    # the full 50,000 and the surplus never becomes customer credit.
    Path("app/services/billing/providers.py"),
}

# --- VasWalletEntry ----------------------------------------------------------
# The wallet is a second, append-only money system. It is currently the
# BEST-behaved one in the codebase — a single writer — and the registry never
# named it. Lock that in while it is still true.
APPROVED_VAS_WALLET_WRITERS = {
    Path("app/services/vas_wallet.py"),
}

# --- Invoice lifecycle -------------------------------------------------------
# ALLOWED_INVOICE_TRANSITIONS exists and is enforced at exactly ONE of the writers
# below (`Invoices.update`). A transition table enforced at one call site is
# decoration, not a guard.
APPROVED_INVOICE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/invoices.py"),
    Path("app/services/billing/_common.py"),
    Path("app/services/billing_automation.py"),
    Path("app/services/billing/reconcile_unposted.py"),
    Path("app/services/collections/_core.py"),
    # DEBT. Repair tools that set terminal invoice state directly instead of
    # calling Invoices.void — so no reversing ledger entries are posted.
    Path("app/services/billing_cleanup_remediation.py"),
    Path("app/services/billing_prepaid_overlap_repair.py"),
    # DEBT. The web layer assigning invoice status directly: skips the legal
    # transition guard AND the invoice_sent event, so bulk-issued invoices emit no
    # webhook and no canonical notification.
    Path("app/services/web_billing_invoice_bulk.py"),
    # Docstring examples only; no runtime write.
    Path("app/services/locking.py"),
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
        if rel in approved:
            continue
        # The model definitions themselves are not writers.
        if rel.parts[:2] == ("app", "models"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{rel}:{line}")
    return found


def test_only_approved_modules_post_to_the_ledger() -> None:
    violations = _violations(LEDGER_ENTRY_CONSTRUCTION, APPROVED_LEDGER_WRITERS)
    assert not violations, (
        "LedgerEntry constructed outside an approved money owner. The ledger is "
        "the record of money moving; a new posting site needs an explicit "
        "ownership decision, not an import:\n  " + "\n  ".join(violations)
    )


def test_only_the_payment_owner_allocates_payments() -> None:
    violations = _violations(
        PAYMENT_ALLOCATION_CONSTRUCTION, APPROVED_ALLOCATION_WRITERS
    )
    assert not violations, (
        "PaymentAllocation constructed outside the payment owner. An allocation "
        "and the ledger credit that justifies it must move together — never one "
        "without the other:\n  " + "\n  ".join(violations)
    )


def test_only_the_wallet_owner_writes_wallet_entries() -> None:
    violations = _violations(VAS_WALLET_ENTRY_CONSTRUCTION, APPROVED_VAS_WALLET_WRITERS)
    assert not violations, (
        "VasWalletEntry constructed outside vas_wallet.py. The wallet is a second "
        "money system and currently has exactly one writer; keep it that way:\n  "
        + "\n  ".join(violations)
    )


def test_only_approved_modules_transition_invoice_status() -> None:
    violations = _violations(INVOICE_STATUS_WRITE, APPROVED_INVOICE_LIFECYCLE_WRITERS)
    assert not violations, (
        "Invoice status assigned outside an approved lifecycle writer. Route it "
        "through Invoices.update, which enforces ALLOWED_INVOICE_TRANSITIONS and "
        "emits the lifecycle event:\n  " + "\n  ".join(violations)
    )


def test_the_allowlists_are_debt_and_only_shrink() -> None:
    """Every allowlisted path must exist.

    A stale entry is a silent hole: the file was renamed or deleted, and the
    allowlist keeps forgiving a write that no longer happens there — while
    forgiving nothing about wherever the code actually moved to.
    """
    missing: list[str] = []
    for allowlist in (
        APPROVED_LEDGER_WRITERS,
        APPROVED_ALLOCATION_WRITERS,
        APPROVED_VAS_WALLET_WRITERS,
        APPROVED_INVOICE_LIFECYCLE_WRITERS,
    ):
        for rel in allowlist:
            if not (PROJECT_ROOT / rel).exists():
                missing.append(str(rel))
    assert not missing, (
        "allowlisted money writers that no longer exist — remove them, they are "
        "forgiving nothing and hiding the real writer:\n  " + "\n  ".join(missing)
    )
