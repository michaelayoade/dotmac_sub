from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/account_credit_invoice_reconciliation.py"
ADAPTER = ROOT / "scripts/billing/reconcile_account_credit_invoice.py"


def test_reconciliation_is_a_registered_typed_owner() -> None:
    service = service_relationship("financial.account_credit_invoice_reconciliation")

    assert service.module == "app.services.account_credit_invoice_reconciliation"
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "coordinator_managed"
    assert service.contract.events is not None
    assert "account_credit.invoice_reconciled" in service.contract.events.event_types


def test_owner_has_one_transaction_boundary_and_adapter_has_none() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    adapter = ADAPTER.read_text(encoding="utf-8")

    assert owner.count("execute_owner_command(") == 1
    assert (
        "AccountCreditApplications.apply_invoice_from_selected_payment_fully(" in owner
    )
    assert "Payment(" not in owner
    assert "db.commit(" not in owner
    assert "db.rollback(" not in owner
    assert "execute_owner_command(" not in adapter
    assert "Payment(" not in adapter
    assert ".commit(" not in adapter
    assert ".rollback(" not in adapter


def test_operator_is_preview_only_unless_apply_is_explicit() -> None:
    adapter = ADAPTER.read_text(encoding="utf-8")

    assert 'action="store_true"' in adapter
    assert "if not args.apply:" in adapter
    assert "expected_preview_fingerprint=args.expected_preview_fingerprint" in adapter
