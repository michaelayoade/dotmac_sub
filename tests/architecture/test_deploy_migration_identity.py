"""Structural guards for the production migration identity.

Behaviour is executed in ``tests/test_deploy_migrator_identity.py`` and
``tests/test_migration_identity.py``. These guards pin the shape that keeps the
migrator credential away from every runtime container:

- it is attached to the Alembic ``compose run`` and to no other command;
- no compose service and no ``.env.example`` declares migrator material;
- the ``.env`` refusal runs before any database work;
- the required CI job is production-shaped, and its sensitivity check exists.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    MIGRATOR_MEMBERSHIPS,
    MIGRATOR_ROLE,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
ROLES_SCRIPT = ROOT / "scripts" / "ci" / "production_roles_migration.py"

#: Names that must exist only on the Alembic one-off container.
MIGRATOR_ENV_NAMES = (
    "MIGRATION_DATABASE_URL",
    "MIGRATION_SCHEMA_OWNER",
    "MIGRATION_PRODUCTION_MODE",
    "MIGRATOR_PGPASS",
    "PGPASSFILE",
)


def _code(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start)
    return source[start:end]


def test_the_contract_pins_the_migrator_and_keeps_the_runtime_role_unprivileged() -> (
    None
):
    migrator = COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[MIGRATOR_ROLE]
    assert (migrator.can_login, migrator.bypass_rls, migrator.superuser) == (
        True,
        True,
        False,
    )
    # The deploy preflight keeps this: a BYPASSRLS runtime role is refused.
    app = COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT["dotmac_app"]
    assert app.bypass_rls is False
    assert set(MIGRATOR_MEMBERSHIPS) == {"dotmac_app", "app_admin"}
    assert all(admin is False for _, _, admin in MIGRATOR_MEMBERSHIPS.values())


def test_the_credential_is_attached_only_to_the_alembic_container() -> None:
    deploy = _code(DEPLOY.read_text(encoding="utf-8"))

    # Where the flags are DEFINED: exactly one function.
    definition = _function(deploy, "migration_identity_args")
    for needle in ("PGPASSFILE=", "MIGRATION_DATABASE_URL=", "MIGRATOR_PGPASS_MOUNT"):
        assert needle in definition

    # Where they are EXPANDED: exactly one place, the Alembic `compose run`.
    expansions = [
        line for line in deploy.splitlines() if "MIGRATION_IDENTITY_ARGS[@]" in line
    ]
    # Two occurrences share one line: the `+` guard and its expansion.
    assert len(expansions) == 1, expansions
    run_migrations = _function(deploy, "run_migrations")
    assert "MIGRATION_IDENTITY_ARGS[@]" in run_migrations
    assert "alembic upgrade heads" in run_migrations

    # No other `compose run` (bootstrap, verify, schema contracts, candidates)
    # may carry the migrator material.
    for match in re.finditer(
        r"\"\$\{COMPOSE\[@\]\}\" run[^\n]*(?:\\\n[^\n]*)*", deploy
    ):
        block = match.group(0)
        if "alembic upgrade heads" in block:
            continue
        for name in MIGRATOR_ENV_NAMES:
            if name == "PGPASSFILE" and "SCHEMA_BOOTSTRAP_PGPASS_MOUNT" in block:
                continue  # the separate, pre-existing schema-bootstrap credential
            if name == "MIGRATION_DATABASE_URL" and "--verify-only" in block:
                continue  # restricted read-only verify: runtime URL, no secret
            assert name not in block, (name, block)

    # The mount of the migrator pgpass is read-only.
    assert re.search(
        r'-v "\$\{MIGRATOR_PGPASS\}:\$\{MIGRATOR_PGPASS_MOUNT\}:ro"', deploy
    )


def test_no_compose_service_or_env_example_declares_migrator_material() -> None:
    compose: dict[str, Any] = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    for name, service in compose["services"].items():
        rendered = yaml.safe_dump(service or {})
        for env_name in MIGRATOR_ENV_NAMES:
            assert env_name not in rendered, (
                f"compose service {name} declares {env_name}; only the Alembic "
                "one-off container may hold migrator material"
            )
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for env_name in MIGRATOR_ENV_NAMES:
        assert not re.search(rf"^\s*{env_name}\s*=", example, re.MULTILINE), env_name
    assert f"{MIGRATOR_ROLE}:" not in example


def test_the_env_refusal_runs_before_any_database_work() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    refusal = _function(deploy, "require_migrator_identity")
    assert "MIGRATION_DATABASE_URL" in refusal
    assert "DEPLOY REFUSED" in refusal
    assert "exit 1" in refusal

    call = re.search(r"^require_migrator_identity$", deploy, re.MULTILINE)
    assert call is not None
    assert call.start() < deploy.index("\nrun_database_prerequisite_bootstrap\n")
    assert call.start() < deploy.index("Backing up database before migrations")


def test_production_only_and_staging_is_not_changed() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    for function in ("require_migrator_identity", "migration_identity_args"):
        body = _function(deploy, function)
        assert '"${DEPLOYMENT_TARGET:-}" == "production"' in body, function


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_the_required_ci_job_exists_and_reports_on_every_run() -> None:
    job = _workflow()["jobs"]["migrations-production-roles"]
    assert job["name"] == "Migrations (production roles)"
    # A required context that does not report deadlocks the merge queue.
    assert str(job["if"]).startswith("always()")
    assert "postgresql-required" not in str(job["if"])
    assert "postgis/postgis:16" in job["services"]["postgres"]["image"]
    runs = [str(step.get("run", "")) for step in job["steps"]]
    assert any("scripts.ci.production_roles_migration" in run for run in runs)
    guarded = [step for step in job["steps"] if "if" in step]
    assert len(guarded) == len(job["steps"]), (
        "every step must guard itself so the context reports on docs-only runs"
    )


def test_the_rehearsal_never_migrates_as_a_superuser_and_keeps_its_sensitivity() -> (
    None
):
    source = ROLES_SCRIPT.read_text(encoding="utf-8")
    code = _code(source)
    # Alembic is only ever invoked with the migrator or the runtime role.
    for call in re.finditer(r"\balembic\(\s*([^)]*?)\)", code, re.DOTALL):
        assert "ADMIN_USER" not in call.group(1), call.group(0)
    # The sensitivity run: same chain as dotmac_app, expected to FAIL at bi_0006.
    assert "SENSITIVITY_DB" in code and "expect_ok=False" in code
    assert "bi_0006_cash_source_claim" in code
    assert "InsufficientPrivilege" in code
    # Credentials come from a 0400 pgpass file, never a URL or print.
    assert "chmod(0o400)" in code
    assert not re.search(r"postgresql[^\n\"']*://[^\n\"']*:[^\n\"'@/]+@", code)
    # Seeds one confirmed settlement and checks the backfill.
    assert "confirmed_settlements" in code and "cash_source_claims" in code
