"""Static ownership guards for the generic Integrator settlement port."""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "app/api/integrator_observations.py"
ADMIN_API = ROOT / "app/api/integrations.py"
OWNER = ROOT / "app/services/payment_webhook_commands.py"
MAPPING_OWNER = ROOT / "app/services/payment_gateway_finance.py"
MIGRATION = ROOT / "alembic/versions/550_integrator_provider_ref.py"


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _name_calls(node: ast.AST, name: str) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    )


def test_settlement_route_is_a_transaction_neutral_adapter() -> None:
    route = _function(API, "receive_integrator_settlement")
    calls = _calls(route)

    assert "process_integrator_settlement" in calls
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "begin_nested" not in calls
    assert "PaymentProvider" not in API.read_text(encoding="utf-8")


def test_settlement_owner_has_no_provider_specific_branch() -> None:
    source = OWNER.read_text(encoding="utf-8")
    function = _function(OWNER, "_process_integrator_settlement")
    provider_lookup = _function(OWNER, "_integrator_provider")
    literals = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "paystack" not in literals
    assert "flutterwave" not in literals
    assert "connector_key" not in ast.unparse(provider_lookup)
    assert "process_paystack_webhook" not in source
    assert "process_flutterwave_webhook" not in source


def test_settlement_owner_owns_one_complete_transaction() -> None:
    public = _function(OWNER, "process_integrator_settlement")
    implementation = _function(OWNER, "_process_integrator_settlement")

    assert _name_calls(public, "execute_owner_command") == 1
    assert not {"commit", "rollback", "begin_nested"} & _calls(public)
    assert not {"commit", "rollback", "begin_nested"} & _calls(implementation)


def test_integrator_provider_reference_migration_extends_the_current_chain() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"), filename=str(MIGRATION))
    assignments = {
        node.target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert assignments["revision"] == "550_integrator_provider_ref"
    assert assignments["down_revision"] == "549_gateway_intent_lifecycle"

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic/versions"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert any(
        revision.revision == assignments["revision"]
        for revision in script.iterate_revisions(
            heads[0], assignments["revision"], inclusive=True
        )
    )


def test_mapping_is_nullable_unique_and_never_payload_selected() -> None:
    model = (ROOT / "app/models/billing.py").read_text(encoding="utf-8")
    schema = (ROOT / "app/schemas/integrator_settlement_observation.py").read_text(
        encoding="utf-8"
    )
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "integrator_installation_ref" in model
    assert "unique=True" in model
    assert "uq_payment_providers_integrator_installation_ref" in migration
    assert "provider_id:" not in schema


def test_mapping_has_one_operator_adapter_and_one_finance_writer() -> None:
    adapter = _function(ADMIN_API, "bind_integrator_payment_provider")
    owner = _function(MAPPING_OWNER, "bind_integrator_installation")

    assert "bind_integrator_installation" in _calls(adapter)
    assert not any(
        isinstance(node, ast.Name) and node.id == "PaymentProvider"
        for node in ast.walk(adapter)
    )
    assert "integrator_installation_ref" in ast.unparse(owner)
    assert "commit" not in _calls(owner)
    assert "rollback" not in _calls(owner)
