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
        "billing.legacy_retained_columns.staged",
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


def test_attachment_population_and_exact_migration_557_helper_policy() -> None:
    from app.services.billing.legacy_privilege_closure import (
        _RELAY_HELPERS,
        ClosurePopulation,
        _population,
    )

    assert _population(ClosurePopulation.ARCHIVE) == tuple(
        sorted(
            [relation.value for relation in ArchiveRelation]
            + ["outbox_events", "platform_outbox_events"]
        )
    )
    assert len(_population(ClosurePopulation.ARCHIVE)) == 46
    migration = ast.parse(
        (ROOT / "alembic/versions/557_outbox_relay_prereq.py").read_text()
    )
    create_helpers = next(
        node
        for node in migration.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_relay_functions"
    )
    statements = [
        node.args[0].value
        for node in ast.walk(create_helpers)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert len(statements) == len(_RELAY_HELPERS) == 4
    expected_dispatchers = {
        "claim_outbox_batch": "outbox_dispatcher",
        "settle_outbox_event": "outbox_dispatcher",
        "claim_platform_outbox_batch": "platform_outbox_dispatcher",
        "settle_platform_outbox_event": "platform_outbox_dispatcher",
    }
    for policy, statement in zip(_RELAY_HELPERS, statements, strict=True):
        parsed = re.fullmatch(
            r"\s*CREATE OR REPLACE FUNCTION public\.(\w+)\((.*?)\)\s*"
            r"RETURNS (.*?)\s+LANGUAGE (\w+)\s+SECURITY DEFINER\s+"
            r"SET search_path = ''\s+AS \$fn\$(.*?)\$fn\$;\s*",
            statement,
            re.DOTALL,
        )
        assert parsed is not None
        name, arguments, returns, language, body = parsed.groups()
        assert policy.name == name
        assert policy.arguments == " ".join(arguments.split())
        assert policy.returns == returns
        assert policy.language == language
        assert policy.body == body
        assert policy.dispatcher == expected_dispatchers[name]


def test_shared_closure_is_private_flush_only_and_cannot_repair_topology() -> None:
    module = ROOT / "app/services/billing/legacy_privilege_closure.py"
    tree = ast.parse(module.read_text())
    assert not [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        assert not re.search(
            r"\b(?:CASCADE|SET\s+(?:LOCAL\s+)?ROLE|SET\s+SESSION\s+AUTHORIZATION)\b",
            node.value,
            re.IGNORECASE,
        )
        assert not re.match(r"\s*(?:CREATE|ALTER|GRANT)\s", node.value, re.IGNORECASE)
    assert "RESTRICT" in module.read_text()
    owner = service_relationship("runtime.billing_legacy_archive_fence")
    assert (
        "app.services.billing.legacy_privilege_closure" in owner.implementation_modules
    )


def test_archive_locks_observes_and_closes_before_archive_mutation() -> None:
    tree = ast.parse(MODULE.read_text())
    installer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_install_legacy_archive_fence"
    )
    positions = {
        name: min(
            node.lineno
            for node in ast.walk(installer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )
        for name in (
            "_lock_privilege_population",
            "_observe_privilege_closure",
            "_close_attachment_privileges",
        )
    }
    assert (
        positions["_lock_privilege_population"]
        < positions["_observe_privilege_closure"]
        < positions["_close_attachment_privileges"]
    )
