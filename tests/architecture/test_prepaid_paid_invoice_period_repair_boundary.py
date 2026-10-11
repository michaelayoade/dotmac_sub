"""Ownership guards for the finance-reviewed paid-invoice period repair."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.services import prepaid_paid_invoice_period_repair as owner
from app.services.billing.invoices import Invoices
from app.services.prepaid_coverage_quarantine_review import ResolutionRoute
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "billing" / "repair_prepaid_paid_invoice_period.py"


def test_repair_has_one_contracted_owner():
    service = service_relationship(owner.OWNER)

    assert service.module == owner.__name__
    assert service.owns == (owner.CONCERN,)
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.NATIVE
    (concern,) = service.contract.concerns
    assert concern.role is OwnerRole.RECONCILER
    assert concern.canonical_writer == owner.OWNER
    assert service.contract.events is not None
    assert set(service.contract.events.event_types) == {
        "prepaid_paid_invoice_period_repair.requested",
        "prepaid_paid_invoice_period.repaired",
    }
    for path in service.contract.test_refs + service.contract.design_refs:
        assert (ROOT / path).exists(), path


def _assigned_attributes(source: str) -> set[str]:
    assigned: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        assigned.update(
            target.attr for target in targets if isinstance(target, ast.Attribute)
        )
    return assigned


def test_owner_writes_only_through_participants_and_the_entitlement_writer():
    source = inspect.getsource(owner)

    assert source.count("execute_owner_command(") == 2
    assert "Invoices.restore_reviewed_paid_prepaid_period_for_owner(" in source
    assert "ensure_prepaid_entitlement_for_paid_invoice_line(" in source
    # No ad hoc entitlement, money, or documentary writes.
    for forbidden in (
        "ServiceEntitlement(",
        "PaymentAllocation(",
        "LedgerEntry(",
        "AccountAdjustment(",
        ".commit(",
        ".rollback(",
        "begin_nested(",
        "db.add(",
    ):
        assert forbidden not in source, forbidden
    assert not _assigned_attributes(source) & {
        "billing_period_start",
        "billing_period_end",
        "subscription_id",
        "metadata_",
        "status",
        "amount",
        "next_billing_at",
        "starts_at",
        "ends_at",
    }
    # Evidence is structured: memo/description text is never read.
    assert ".memo" not in source
    assert ".description" not in source


def test_invoice_participant_is_flush_only_and_keeps_money_and_kind():
    source = inspect.getsource(Invoices.restore_reviewed_paid_prepaid_period_for_owner)

    assert "db.flush()" in source
    assert "commit(" not in source
    assert "rollback(" not in source
    for forbidden in (
        "invoice.status =",
        "invoice.total =",
        "invoice.balance_due =",
        "invoice.subtotal =",
        "line.amount =",
        '"kind"',
    ):
        assert forbidden not in source, forbidden


def test_cli_is_an_adapter():
    tree = ast.parse(CLI.read_text())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls & {"commit", "rollback", "add", "delete", "flush", "execute"}
    source = CLI.read_text()
    assert "execute_owner_command" not in source
    assert "approve_paid_invoice_period_repair(" in source
    assert "request_paid_invoice_period_repair(" in source


def test_quarantine_review_routes_malformed_periods_to_this_owner():
    source = inspect.getsource(
        __import__(
            "app.services.prepaid_coverage_quarantine_review",
            fromlist=["_invoice_options"],
        )
    )

    assert ResolutionRoute.reviewed_paid_invoice_period_repair.value in source
    assert owner.OWNER in source
    assert "engineering_paid_invoice_period_restoration" not in source
    assert "engineering_documentary_paid_invoice_period" not in source
