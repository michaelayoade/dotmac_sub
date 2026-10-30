"""The migrator/owner role graph points one way, everywhere it is encoded.

``app_admin`` (the BYPASSRLS migrator) is a member of ``dotmac_app`` (the runtime
owner). Encoding the old edge (``dotmac_app`` a member of ``app_admin``) anywhere
- the verify contract, the elevated repair, the restore drill fixture, the 557
guard - would either refuse every deploy or silently restore a SET ROLE path from
the runtime identity to a BYPASSRLS role.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.outbox_dispatcher_roles import OUTBOX_RELAY_OWNERSHIP_CONTRACT

ROOT = Path(__file__).resolve().parents[2]
OLD_EDGE = re.compile(r"GRANT\s+\"?app_admin\"?\s+TO\s+\"?dotmac_app\"?", re.IGNORECASE)
SCANNED = (
    "scripts/bootstrap_outbox_dispatcher_roles.py",
    "scripts/bootstrap_commercial_module_prereqs.py",
    "scripts/restore_drill.py",
    "scripts/deploy.sh",
    "scripts/ci/migrations_production_roles.py",
    "alembic/env.py",
    "alembic/versions/557_outbox_relay_prereq.py",
    "app/outbox_dispatcher_roles.py",
    "app/migration_ownership.py",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_contract_declares_the_new_direction() -> None:
    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    assert (contract.migration_role, contract.owner_role) == ("app_admin", "dotmac_app")


def test_no_executable_source_grants_the_old_direction() -> None:
    offenders = [name for name in SCANNED if OLD_EDGE.search(_read(name))]
    assert offenders == []


def test_restore_drill_fixture_uses_the_new_edge() -> None:
    source = _read("scripts/restore_drill.py")
    assert "GRANT dotmac_app TO app_admin WITH INHERIT TRUE, SET TRUE;" in source
    assert "migrator_owner_direction" in source


def test_557_guard_asks_whether_the_runner_can_set_the_definer_role() -> None:
    source = _read("alembic/versions/557_outbox_relay_prereq.py")
    assert "pg_has_role(current_user, :role, 'SET')" in source
    assert "is not a member of {DEFINER_ROLE}" not in source


def test_bootstrap_repair_never_builds_the_old_edge_from_the_contract() -> None:
    source = _read("scripts/bootstrap_outbox_dispatcher_roles.py")
    # The only GRANT of a role in this script is owner -> migrator.
    assert 'sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(' in source
    grant = source[source.index('sql.SQL("GRANT {} TO {} WITH INHERIT') :]
    assert grant.index("owner, migrator") < grant.index("dry_run=dry_run")


def test_the_deploy_grants_a_fixed_runtime_role() -> None:
    deploy = _read("scripts/deploy.sh")
    assert 'MIGRATION_RUNTIME_ROLE="dotmac_app"' in deploy
    assert '-e MIGRATION_RUNTIME_ROLE="${MIGRATION_RUNTIME_ROLE}"' in deploy
    assert "OWNER TO" not in _read("app/migration_ownership.py")


def test_env_py_is_opt_in_and_names_the_contract() -> None:
    env = _read("alembic/env.py")
    assert "runtime_role_from_environ" in env
    ownership = _read("app/migration_ownership.py")
    assert 'CONTRACT_NAME: Final = "module_database_roles.v1"' in ownership


def test_a_definer_allowlist_exists_and_lists_only_relay_functions() -> None:
    lines = [
        line
        for line in _read("alembic/definer_owner_allowlist.txt").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(lines) == 4
    assert all(re.match(r"public\.\w*outbox\w*\(", line) for line in lines), lines


def test_production_roles_job_is_part_of_the_required_tests_context() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/ci.yml"))
    job = workflow["jobs"]["migrations-production-roles"]
    assert "postgresql-required" not in str(job.get("if", ""))
    assert "migrations-production-roles" in workflow["jobs"]["test"]["needs"]
    gate = yaml.safe_dump(workflow["jobs"]["test"]["steps"])
    assert "needs.migrations-production-roles.result" in gate
    run = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "scripts.ci.migrations_production_roles" in run
    image = job["services"]["postgres"]["image"]
    assert image.startswith("postgis/postgis:16")


def test_production_roles_lane_audits_and_proves_its_own_sensitivity() -> None:
    lane = _read("scripts/ci/migrations_production_roles.py")
    for needle in (
        "new objects are migrator-owned; relay definers stay with it",
        "runtime role holds DML and sequence grants on every new object",
        "runtime role has no CREATE on public",
        "runtime role cannot create or alter in public",
        "every module table is FORCE row-level security",
        "owner is bound by row-level security",
        "no edge: ALTER alembic_version needs ownership",
        "old edge: ALTER alembic_version needs ownership",
        "repair converges to the new edge only",
        "as the runtime owner: migration is refused",
    ):
        assert needle in lane, needle
    # The migrator is never a superuser in this lane.
    assert "SUPERUSER" not in lane.replace("NOSUPERUSER", "")
