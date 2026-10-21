"""Ownership guards for the finance-reviewed renewal origin correction."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.services import prepaid_renewal_origin_correction as owner
from app.services import service_entitlements
from app.services.billing import adjustments
from app.services.prepaid_coverage_quarantine_review import ResolutionRoute
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "billing" / "correct_prepaid_renewal_origin.py"


def test_correction_has_one_contracted_owner():
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
        "prepaid_renewal_origin.corrected"
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

    assert source.count("execute_owner_command(") == 1
    assert "stage_reviewed_renewal_origin_ref_correction_for_owner(" in source
    assert "link_prepaid_entitlement_to_funding_debit_for_owner(" in source
    assert "ensure_prepaid_entitlement_for_wallet_debit(" in source
    # No ad hoc entitlement, money, or documentary writes.
    for forbidden in (
        "ServiceEntitlement(",
        "PaymentAllocation(",
        "LedgerEntry(",
        "AccountAdjustment(",
        "LedgerEntries.",
        ".commit(",
        ".rollback(",
        "begin_nested(",
        "db.add(",
    ):
        assert forbidden not in source, forbidden
    assert not _assigned_attributes(source) & {
        "origin_ref",
        "source_ledger_entry_id",
        "metadata_",
        "status",
        "amount",
        "amount_funded",
        "starts_at",
        "ends_at",
        "reversed_at",
        "next_billing_at",
    }
    # Evidence is structured: memo/description/reason text is never read.
    assert ".memo" not in source
    assert ".description" not in source
    assert "adjustment.reason" not in source


def test_adjustment_participant_is_flush_only_and_changes_only_the_reference():
    source = inspect.getsource(
        adjustments.stage_reviewed_renewal_origin_ref_correction_for_owner
    )

    assert "owner_command_active(" in source
    assert "db.flush()" in source
    assert "commit(" not in source
    assert "rollback(" not in source
    assert _assigned_attributes(source) == {"origin_ref"}


def test_entitlement_participant_is_flush_only_and_adds_only_the_debit_link():
    source = inspect.getsource(
        service_entitlements.link_prepaid_entitlement_to_funding_debit_for_owner
    )

    assert "owner_command_active(" in source
    assert "db.flush()" in source
    assert "commit(" not in source
    assert "rollback(" not in source
    assert _assigned_attributes(source) == {"source_ledger_entry_id", "metadata_"}
    for forbidden in ("starts_at", "ends_at", "amount_funded", "status", "currency"):
        assert f"entitlement.{forbidden} =" not in source


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
    assert "correct_renewal_origin(" in source
    assert "preview_renewal_origin_correction(" in source


def test_quarantine_review_routes_legitimate_debits_to_this_owner():
    source = inspect.getsource(
        __import__(
            "app.services.prepaid_coverage_quarantine_review",
            fromlist=["_origin_correction_option"],
        )
    )

    assert ResolutionRoute.reviewed_renewal_origin_correction.value in source
    assert owner.OWNER in source
