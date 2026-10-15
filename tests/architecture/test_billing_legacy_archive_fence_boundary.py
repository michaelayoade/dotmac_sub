"""Private archive policy cannot silently expand into activation or shared state."""

import ast
import re
from pathlib import Path

from app.services.billing.legacy_archive_fence_contracts import ArchiveRelation
from app.services.sot_manifest import TransactionMode
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/billing/legacy_archive_fence.py"


def test_closed_inventory_is_exactly_the_44_archived_tables() -> None:
    inventory = (ROOT / "docs/designs/BILLING_LEGACY_FENCE_INVENTORY.md").read_text()
    archived = inventory.split("## Archived monetary records and evidence", 1)[1].split(
        "`import_runs`", 1
    )[0]
    assert {item.value for item in ArchiveRelation} == set(
        re.findall(r"`([a-z_]+)`", archived)
    )
    assert len(ArchiveRelation) == 44
    assert not {
        "subscribers",
        "billing_accounts",
        "topup_intents",
        "event_store",
        "payment_proofs",
        "withholding_tax_records",
    } & {item.value for item in ArchiveRelation}


def test_private_participant_has_no_transaction_or_activation_authority() -> None:
    tree = ast.parse(MODULE.read_text())
    public = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert public == set()
    forbidden = {
        "commit",
        "rollback",
        "begin",
        "begin_nested",
        "execute_owner_command",
        "create_engine",
        "sessionmaker",
        "Session",
    }
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert not forbidden & calls
    service = service_relationship("runtime.billing_legacy_archive_fence")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert service.depends_on == ("events.owner_outputs",)
    assert service.contract.events is not None
    assert service.contract.events.event_types == (
        "billing.legacy_archive_fence.staged",
    )


def test_no_production_caller_installs_or_exposes_private_fence() -> None:
    for directory in ("app", "alembic", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            if path == MODULE:
                continue
            tree = ast.parse(path.read_text())
            assert not any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "_install_legacy_archive_fence"
                    )
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_install_legacy_archive_fence"
                    )
                )
                for node in ast.walk(tree)
            ), path


def test_observation_is_staged_and_has_no_admission_flags() -> None:
    source = (
        ROOT / "app/services/billing/legacy_archive_fence_contracts.py"
    ).read_text()
    tree = ast.parse(source)
    observation = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ArchiveFenceStagedObservation"
    )
    names = {
        node.target.id
        for node in observation.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert {"transaction_id", "database_oid", "backend_pid", "catalog_digest"} <= names
    assert (
        not {"ready", "active", "activated", "full_closure", "handoff_complete"} & names
    )
