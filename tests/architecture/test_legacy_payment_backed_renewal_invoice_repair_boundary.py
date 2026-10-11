from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/prepaid_service_renewals.py"
LEDGER = ROOT / "app/services/billing/ledger.py"
PAYMENTS = ROOT / "app/services/billing/payments.py"
ACCOUNT_CREDIT = ROOT / "app/services/billing/account_credit.py"
CLI = ROOT / "scripts/billing/repair_legacy_payment_backed_renewal_invoice.py"
REGISTRY = ROOT / "app/services/sot_registry/domains/financial_access/prepaid.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


def test_repair_has_typed_query_preview_command_and_result_contracts():
    source = OWNER.read_text(encoding="utf-8")
    for declaration in (
        "class LegacyPaymentBackedRenewalInvoiceRepairQuery:",
        "class LegacyPaymentBackedRenewalInvoiceRepairPreview:",
        "class RepairLegacyPaymentBackedRenewalInvoiceCommand:",
        "class LegacyPaymentBackedRenewalInvoiceRepairResult:",
        "def preview_legacy_payment_backed_renewal_invoice_repair(",
        "def repair_legacy_payment_backed_renewal_invoice(",
    ):
        assert declaration in source


def test_public_repair_enters_owner_command_once_and_participants_are_flush_only():
    repair = _function_source(OWNER, "repair_legacy_payment_backed_renewal_invoice")
    preview = _function_source(
        OWNER, "preview_legacy_payment_backed_renewal_invoice_repair"
    )
    ledger_participant = _function_source(
        LEDGER, "stage_legacy_payment_backed_renewal_reversal"
    )
    payment_participant = _function_source(
        PAYMENTS, "stage_confirm_legacy_prepaid_debit_replacement_for_owner"
    )
    account_credit_participant = _function_source(
        ACCOUNT_CREDIT, "apply_invoice_from_legacy_prepaid_debit_fully"
    )
    assert repair.count("execute_owner_command(") == 1
    assert "stage_legacy_payment_backed_renewal_reversal(" in repair
    assert "stage_system_invoice_for_owner(" in repair
    assert "stage_system_line_for_owner(" in repair
    assert ".commit(" not in repair
    assert ".rollback(" not in repair
    assert "commit=False" in ledger_participant
    assert ".commit(" not in ledger_participant
    assert ".rollback(" not in ledger_participant
    assert "begin_nested(" not in ledger_participant
    assert "PaymentAllocations._confirm(" in payment_participant
    assert "complete_transaction=False" in payment_participant
    assert ".commit(" not in payment_participant
    assert ".rollback(" not in payment_participant
    assert "_apply_invoice_from_selected_payment_fully(" in account_credit_participant
    assert ".commit(" not in account_credit_participant
    assert ".rollback(" not in account_credit_participant
    assert "db.add(" not in preview
    assert "db.flush(" not in preview
    assert ".commit(" not in preview
    assert ".rollback(" not in preview


def test_cli_is_a_dry_run_first_typed_adapter_without_direct_writes():
    source = CLI.read_text(encoding="utf-8")
    assert 'parser.add_argument("--apply", action="store_true")' in source
    for required in (
        "--preview-fingerprint",
        "--actor-user-id",
        "--permission-evidence",
        "--reason-evidence-reference",
        "--idempotency-key",
    ):
        assert required in source
    assert "preview_legacy_payment_backed_renewal_invoice_repair(" in source
    assert "repair_legacy_payment_backed_renewal_invoice(" in source
    assert "db.add(" not in source
    assert "db.execute(" not in source
    assert "db.commit(" not in source
    assert "db.rollback(" not in source


def test_complete_sot_contract_registers_the_repair_concern():
    source = REGISTRY.read_text(encoding="utf-8")
    concern = "reviewed legacy payment-backed prepaid renewal invoice repair"
    assert source.count(f'"{concern}"') >= 2
    assert 'name="financial.prepaid_service_renewals"' in source
    assert 'owner="financial.ledger"' in source
    assert 'owner="auth.permission_gate"' in source
    assert "TransactionMode.OWNER_MANAGED" in source
