"""Ownership guards for the finance-reviewed legacy over-allocation return."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from app.services.billing import legacy_over_allocation_correction as owner
from app.services.billing.payments import PaymentAllocations
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "billing" / "return_legacy_over_allocation.py"


def test_return_has_one_contracted_owner():
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
        "payment_allocation.over_allocation_returned"
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


def test_owner_posts_nothing_and_writes_only_through_the_payment_participant():
    source = inspect.getsource(owner)

    assert source.count("execute_owner_command(") == 1
    assert "stage_reviewed_legacy_over_allocation_return_for_owner(" in source
    # The ledger already holds the credit; a reversal posting would double it.
    for forbidden in (
        "LedgerEntry(",
        "LedgerEntries.",
        "PaymentAllocation(",
        "CreditNoteApplication(",
        ".commit(",
        ".rollback(",
        "begin_nested(",
        "db.add(",
        "_finalize_invoice_payment_effects",
        "_recalculate_invoice_totals",
    ):
        assert forbidden not in source, forbidden
    assert not _assigned_attributes(source) & {
        "is_active",
        "reversed_at",
        "amount",
        "status",
        "balance_due",
        "total",
    }


def test_payment_participant_is_flush_only_and_never_posts_money():
    source = textwrap.dedent(
        inspect.getsource(
            PaymentAllocations.stage_reviewed_legacy_over_allocation_return_for_owner
        )
    )

    assert "owner_command_active(" in source
    assert "db.flush()" in source
    assert "commit(" not in source
    assert "rollback(" not in source
    assert _assigned_attributes(source) == {
        "is_active",
        "reversed_at",
        "reversal_preview_fingerprint",
        "reversal_idempotency_key",
        "reversal_reason",
        "reversal_actor_id",
        "updated_at",
    }
    for forbidden in ("LedgerEntry(", "LedgerEntries.", "invoice.status ="):
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
    assert "return_legacy_over_allocation(" in source
    assert "preview_legacy_over_allocation_return(" in source
