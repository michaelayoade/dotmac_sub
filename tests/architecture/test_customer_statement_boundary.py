"""The customer statement cannot acquire archived or admin ledger authority."""

import ast
from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_registry.registry import all_services

ROOT = Path(__file__).resolve().parents[2]
STATEMENT_SOURCE = ROOT / "app" / "services" / "customer_account_statement.py"


def test_customer_statement_owners_are_separate_from_admin_projection() -> None:
    services = {service.name: service for service in all_services()}
    read_owner = services["customer.account_statement"]
    email_owner = services["customer.account_statement_email"]
    admin_owner = services["ui.billing_account_workspace_projection"]
    assert read_owner.module == "app.services.customer_account_statement"
    assert email_owner.module == "app.services.customer_account_statement_email"
    assert read_owner.contract is not None
    assert email_owner.contract is not None
    assert read_owner.contract.transaction.mode is TransactionMode.READ_ONLY
    assert email_owner.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert admin_owner.module != read_owner.module


def test_customer_statement_has_no_archive_or_admin_ledger_dependency() -> None:
    source = ast.parse(STATEMENT_SOURCE.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(source)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "app.models.splynx_transaction" not in imports
    assert "app.services.web_billing_ledger" not in imports
    assert "app.services.web_billing_statements" not in imports
