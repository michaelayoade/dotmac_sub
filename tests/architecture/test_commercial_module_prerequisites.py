"""Deployment guardrails for composed commercial module prerequisites."""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from pathlib import Path

from app.commercial_module_prereqs import COMMERCIAL_MODULE_SCHEMA_CONTRACT

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"
MIGRATION_546 = ROOT / "alembic" / "versions" / "546_module_db_roles_prereq.py"
DEPLOY = ROOT / "scripts" / "deploy.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_commercial_module_prereqs.py"


def _executed_sql(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
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


def _declared_lineages() -> tuple[str, ...]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    entries = parser["alembic"]["version_locations"].split()
    return tuple(
        entry.removesuffix(".migrations:versions")
        for entry in entries
        if entry.endswith(".migrations:versions")
    )


def test_schema_prerequisite_manifest_matches_the_composed_lineages() -> None:
    contracted_imports = {
        item.import_name for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    }
    assert contracted_imports == set(_declared_lineages())

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        requirement.split("==")[0]
        for requirement in data["project"]["dependencies"]
        if requirement.startswith("dotmac-")
    }
    contracted_distributions = {
        item.distribution for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    }
    assert contracted_distributions <= dependencies


def test_cluster_role_creation_is_owned_by_the_bootstrap_script() -> None:
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "CREATE ROLE" in bootstrap_source
    assert 'sql.SQL("CREATE ROLE {} {}")' in bootstrap_source
    assert 'sql.SQL("ALTER ROLE {} {}")' in bootstrap_source
    assert "BOOTSTRAP_DATABASE_URL" in bootstrap_source
    assert "MIGRATION_DATABASE_URL" in bootstrap_source

    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        sql = _executed_sql(path).upper()
        assert "CREATE ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits CREATE ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )
        assert "ALTER ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits ALTER ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )


def test_546_verifies_module_roles_instead_of_creating_them() -> None:
    source = MIGRATION_546.read_text(encoding="utf-8")
    assert "module_database_role_violations" in source
    assert "_assert_module_database_roles_exist()" in source
    assert "CREATE ROLE" not in _executed_sql(MIGRATION_546)
    assert "scripts/bootstrap_commercial_module_prereqs.py" in source


def test_deploy_preflights_prerequisites_before_backup_and_alembic() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "run_database_prerequisite_bootstrap" in deploy
    assert "verify_database_prerequisites" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --repair" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --repair" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --verify-only" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --verify-only" in deploy

    verify_call = re.search(r"^verify_database_prerequisites$", deploy, re.MULTILINE)
    assert verify_call is not None
    assert verify_call.start() < deploy.index("Backing up database before migrations")
    assert verify_call.start() < deploy.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )
