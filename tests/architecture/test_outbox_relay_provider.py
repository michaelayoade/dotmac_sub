"""Static contract for Sub's product-first ``outbox_relay.v1`` provider."""

from __future__ import annotations

import ast
from pathlib import Path

from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS
from app.outbox_dispatcher_roles import (
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "557_outbox_relay_prereq.py"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_outbox_dispatcher_roles.py"
ERP_SOURCE_COMMIT = "dc10b24af22b1452b9954d4c33ff87a5916a4afe"


def _assignment(name: str) -> ast.expr:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
    raise AssertionError(f"{name} is not assigned in {MIGRATION.name}")


def _executed_sql() -> str:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "execute"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                statements.append(argument.value)
            elif isinstance(argument, ast.JoinedStr):
                statements.append(ast.unparse(argument))
    return "\n".join(statements)


def test_provider_is_ordered_bound_and_traces_to_the_product_source() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "557_outbox_relay_prereq"' in source
    assert 'down_revision: str | None = "556_idempotency_ledger_prereq"' in source
    assert 'REQUIRES = ("outbox_relay.v1",)' in source
    assert "require_prerequisites(op.get_bind(), REQUIRES)" in source
    assert ERP_SOURCE_COMMIT in source

    bindings = {
        binding.prerequisite: binding.provider_revision
        for binding in ASSEMBLY_PREREQUISITE_BINDINGS
    }
    assert bindings["outbox_relay.v1"] == "557_outbox_relay_prereq"


def test_migration_copy_matches_the_runtime_login_posture() -> None:
    names = {
        target.id: node.value.value
        for node in ast.parse(MIGRATION.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    contract = _assignment("DISPATCHER_CONTRACT")
    assert isinstance(contract, ast.Dict)
    copied = {
        names[key.id]
        if isinstance(key, ast.Name)
        else ast.literal_eval(key): ast.literal_eval(value)
        for key, value in zip(contract.keys, contract.values, strict=True)
        if key is not None
    }
    assert copied == dict(RELAY_DISPATCHER_CONTRACT)
    assert copied == {
        "outbox_dispatcher": (True, False, False),
        "platform_outbox_dispatcher": (True, False, False),
    }


def test_migration_copy_matches_the_runtime_function_ownership_prerequisites() -> None:
    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    source = MIGRATION.read_text(encoding="utf-8")

    assert f'MIGRATION_ROLE = "{contract.migration_role}"' in source
    assert f'DEFINER_ROLE = "{contract.definer_role}"' in source
    assert f'RELAY_SCHEMA = "{contract.schema}"' in source
    assert (
        f"DEFINER_SCHEMA_PRIVILEGES = {contract.schema_privileges!r}".replace("'", '"')
        in source
    )


def test_role_creation_is_only_in_the_explicit_bootstrap() -> None:
    assert "CREATE ROLE" not in _executed_sql()
    migration_source = MIGRATION.read_text(encoding="utf-8")
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "_assert_dispatcher_roles_exist()" in migration_source
    assert "CREATE ROLE" in bootstrap_source
    assert 'sql.SQL("CREATE ROLE {} {}")' in bootstrap_source
    assert 'sql.SQL("ALTER ROLE {} {}")' in bootstrap_source
    assert "CREATE ROLE {} PASSWORD" not in bootstrap_source
    assert "ALTER ROLE {} PASSWORD" not in bootstrap_source
    assert "BOOTSTRAP_DATABASE_URL" in bootstrap_source
    assert "MIGRATION_DATABASE_URL" in bootstrap_source


def test_four_definer_functions_are_hardened_before_grant() -> None:
    sql = _executed_sql()
    source = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("CREATE OR REPLACE FUNCTION") == 4
    assert sql.count("SECURITY DEFINER") == 4
    assert sql.count("SET search_path = ''") == 4
    assert source.index("REVOKE ALL ON FUNCTION") < source.index(
        "GRANT EXECUTE ON FUNCTION"
    )


def test_both_plane_postures_and_claim_indexes_are_explicit() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY" in source
    assert "tenant_id = public.app_current_tenant_id()" in source
    assert "public.platform_outbox_events ENABLE ROW LEVEL SECURITY" not in source
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE public.platform_outbox_events FROM app_user"
        in source
    )
    for table in ("outbox_events", "platform_outbox_events"):
        assert f'"ix_{table}_status_available_at"' in source
        assert f'"ix_{table}_status_leased_at"' in source
    assert '"ix_outbox_events_tenant_id"' in source


def test_sensitivity_reads_executed_statements_not_explanatory_prose() -> None:
    assert "CREATE ROLE" in MIGRATION.read_text(encoding="utf-8")
    assert "CREATE ROLE" not in _executed_sql()
    assert "CREATE OR REPLACE FUNCTION" in _executed_sql()
