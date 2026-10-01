"""Fail-closed guards for migration identity and legacy schema ownership."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    MigrationPrincipalObservation,
    ModuleSchemaContract,
    ModuleSchemaObservation,
    commercial_role_authority_violations,
    migration_principal_is_valid,
)
from scripts import bootstrap_commercial_module_prereqs as bootstrap

ROOT = Path(__file__).resolve().parents[1]


def test_owner_mismatch_blocks_before_any_repair(monkeypatch) -> None:
    contract = ModuleSchemaContract(
        module="sample",
        distribution="sample",
        import_name="sample",
        schema="mod_sample",
    )
    observed = ModuleSchemaObservation(
        owner_role="dotmac_app",
        public_privileges=(),
        usage_roles=("app_admin", "app_user", "platform_api"),
        probe_observed=True,
    )
    monkeypatch.setattr(bootstrap, "module_schema_contract", lambda: (contract,))
    monkeypatch.setattr(
        bootstrap, "observe_schemas", lambda _conn: {"mod_sample": observed}
    )
    monkeypatch.setattr(bootstrap, "_all_violations", lambda _conn: ("owner drift",))

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("repair was reached before ownership refusal")

    monkeypatch.setattr(bootstrap, "_bootstrap_roles", unexpected_write)
    monkeypatch.setattr(bootstrap, "_bootstrap_schemas", unexpected_write)
    result = bootstrap.run_bootstrap(object(), dry_run=False, repair=True)
    assert result.outcome is bootstrap.Outcome.BLOCKED
    assert result.exit_code == bootstrap.EXIT_BLOCKED
    assert "reviewed cutover" in (result.blocked_reason or "")


def test_migration_identity_is_checked_before_any_catalog_verification(
    monkeypatch,
) -> None:
    class WrongConnection:
        def execute(self, statement):
            assert "session_user" in statement
            return self

        def fetchone(self):
            return ("postgres", "postgres", True, True, True, True, True, True)

    monkeypatch.setattr(bootstrap, "_all_violations", lambda _conn: unexpected())

    def unexpected():
        raise AssertionError("catalog verification ran with wrong login")

    assert bootstrap.verify(WrongConnection()) == bootstrap.EXIT_BLOCKED


@pytest.mark.parametrize(
    "row",
    [
        None,
        ("app_admin", "app_admin", True),
        ("app_admin", "app_admin", True, True, False, False, False, 1),
    ],
)
def test_verify_refuses_missing_or_malformed_identity_before_catalog_read(
    monkeypatch, row
) -> None:
    class Connection:
        def execute(self, statement):
            assert "session_user" in statement
            return self

        def fetchone(self):
            return row

    def unexpected(_conn):
        raise AssertionError("catalog read ran with invalid principal")

    monkeypatch.setattr(bootstrap, "_all_violations", unexpected)
    assert bootstrap.verify(Connection()) == bootstrap.EXIT_BLOCKED


def test_schema_bootstrap_requires_owner_membership_before_repair(monkeypatch) -> None:
    class NonMemberConnection:
        def execute(self, statement):
            assert "pg_has_role" in statement
            return self

        def fetchone(self):
            return (
                "dotmac_schema_bootstrap",
                "dotmac_schema_bootstrap",
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                True,
            )

    monkeypatch.setattr(bootstrap, "module_schema_contract", lambda: ())
    monkeypatch.setattr(bootstrap, "observe_schemas", lambda _conn: {})
    monkeypatch.setattr(bootstrap, "_all_violations", lambda _conn: ("schema missing",))
    result = bootstrap.run_bootstrap(
        NonMemberConnection(),
        dry_run=False,
        repair=True,
        allow_role_creation=False,
    )
    assert result.outcome is bootstrap.Outcome.BLOCKED
    assert "role membership" in (result.blocked_reason or "")


def test_current_role_contract_detects_cluster_creation_drift() -> None:
    observed = {
        name: contract.authority_posture
        for name, contract in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT.items()
    }
    assert commercial_role_authority_violations(observed) == ()
    observed["app_admin"] = (True, True, False, True, True)
    violations = commercial_role_authority_violations(observed)
    assert any("rolcreatedb" in item and "rolcreaterole" in item for item in violations)
    assert "NOCREATEDB NOCREATEROLE" in bootstrap._attributes(
        COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT["app_admin"]
    )


def test_migration_principal_rejects_cluster_or_database_create() -> None:
    correct = MigrationPrincipalObservation(
        session_user="app_admin",
        current_user="app_admin",
        can_login=True,
        bypass_rls=True,
        superuser=False,
        can_create_database=False,
        can_create_role=False,
        database_create=False,
    )
    assert migration_principal_is_valid(correct)
    assert not migration_principal_is_valid(None)
    for changed in (
        replace(correct, session_user="postgres"),
        replace(correct, current_user="postgres"),
        replace(correct, can_login=False),
        replace(correct, bypass_rls=False),
        replace(correct, superuser=True),
        replace(correct, can_create_database=True),
        replace(correct, can_create_role=True),
        replace(correct, database_create=True),
    ):
        assert not migration_principal_is_valid(changed)
    with pytest.raises(ValueError, match="booleans"):
        replace(correct, database_create=1)


def test_schema_bootstrap_requires_actual_login_and_named_create_before_reads(
    monkeypatch,
) -> None:
    correct = (
        "dotmac_schema_bootstrap",
        "dotmac_schema_bootstrap",
        True,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
    )

    class Connection:
        def __init__(self, identity):
            self.identity = identity

        def execute(self, statement):
            assert "aclexplode(db_catalog.datacl)" in statement
            assert "session_user" in statement
            return self

        def fetchone(self):
            return self.identity

    monkeypatch.setattr(bootstrap, "module_schema_contract", lambda: ())

    def unexpected(_conn):
        raise AssertionError("catalog read ran before principal refusal")

    monkeypatch.setattr(bootstrap, "_all_violations", unexpected)
    monkeypatch.setattr(bootstrap, "observe_schemas", unexpected)
    for index in range(len(correct)):
        invalid = list(correct)
        invalid[index] = "app_admin" if index < 2 else not invalid[index]
        result = bootstrap.run_bootstrap(
            Connection(tuple(invalid)),
            dry_run=False,
            repair=True,
            allow_role_creation=False,
        )
        assert result.outcome is bootstrap.Outcome.BLOCKED
        assert result.exit_code == bootstrap.EXIT_BLOCKED


def test_deploy_refuses_missing_and_persisted_migration_dsn() -> None:
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    leg = source.split("migration_connection_preflight() {", 1)[1].split("\n}", 1)[0]
    script = f"""
env_value() {{
  if [[ "$1" == MIGRATION_DATABASE_URL ]]; then
    printf '%s' "$PERSISTED_MIGRATION_URL"
  else
    printf '%s' "$RUNTIME_URL"
  fi
}}
fake_compose() {{
  [[ "$#" -eq 10 && "$1" == run && "$2" == --rm && "$3" == --no-deps \
    && "$4" == -e && "$5" == DATABASE_PAIR_RUNTIME_URL \
    && "$6" == -e && "$7" == MIGRATION_DATABASE_URL \
    && "$8" == app && "$9" == python \
    && "${{10}}" == scripts/verify_database_connection_pair.py ]] || return 90
  [[ "$DATABASE_PAIR_RUNTIME_URL" == "$RUNTIME_URL" \
    && "$MIGRATION_DATABASE_URL" == "$EXPECTED_MIGRATION_URL" ]] || return 91
  printf 'pair-invoked\n' >&2
  [[ "$PAIR_RESULT" == pass ]]
}}
COMPOSE=(fake_compose)
migration_connection_preflight() {{{leg}
}}
migration_connection_preflight
"""
    for migration_url, persisted, pair_result, expected in (
        ("", "", "pass", "is required"),
        (
            "postgresql://app_admin@db/test",
            "persisted",
            "pass",
            "must not be persisted",
        ),
        ("postgresql://app_admin@db/test", "", "pass", None),
        (
            "postgresql://app_admin@db/test",
            "",
            "fail",
            "did not prove one backend",
        ),
    ):
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MIGRATION_DATABASE_URL": migration_url,
                "PERSISTED_MIGRATION_URL": persisted,
                "RUNTIME_URL": "postgresql://app_user@db/test",
                "EXPECTED_MIGRATION_URL": migration_url,
                "PAIR_RESULT": pair_result,
            },
        )
        if expected is None:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert expected in result.stderr
        assert ("pair-invoked" in result.stderr) == (
            bool(migration_url) and not persisted
        )
        if migration_url:
            assert migration_url not in result.stderr
        assert "postgresql://app_user@db/test" not in result.stderr


def test_deploy_refuses_inherited_runtime_url_before_any_compose_command(
    tmp_path: Path,
) -> None:
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert source.index("refuse_inherited_runtime_url || exit 1") < source.index(
        "COMPOSE=(docker compose"
    )
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / ".env").write_text("APP_IMAGE=synthetic\nGIT_SHA=synthetic\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\nprintf 'invoked\\n' > \"$DOCKER_MARKER\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    marker = tmp_path / "docker-marker"
    for inherited in ("", "postgresql://app_user@wrong-db/other"):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/deploy.sh"), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "DATABASE_URL": inherited,
                "DEPLOY_DIR": str(deploy_dir),
                "DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "DOCKER_MARKER": str(marker),
            },
        )
        assert result.returncode != 0
        assert "inherited DATABASE_URL is forbidden" in result.stderr
        assert not marker.exists()
        if inherited:
            assert inherited not in result.stderr


def test_deploy_runtime_guard_accepts_absent_process_url() -> None:
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    guard = source.split("refuse_inherited_runtime_url() {", 1)[1].split("\n}", 1)[0]
    script = f"""
refuse_inherited_runtime_url() {{{guard}
}}
fake_compose() {{ printf 'compose-invoked\\n' >&2; }}
refuse_inherited_runtime_url || exit 1
fake_compose
"""
    environment = dict(os.environ)
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=environment
    )
    assert result.returncode == 0
    assert result.stderr == "compose-invoked\n"


def test_alembic_requires_actual_admin_before_version_table() -> None:
    source = (ROOT / "alembic/env.py").read_text(encoding="utf-8")
    assert 'os.environ.get("MIGRATION_DATABASE_URL"' in source
    assert "settings.database_url" not in source
    online = source.split("def run_migrations_online()", 1)[1]
    assert online.index("session_user") < online.index("ensure_alembic_version_table")
    assert "current_user" in online
    assert "rolbypassrls" in online
    assert "rolsuper" in online
    assert "rolcreatedb" in online
    assert "rolcreaterole" in online
    assert "has_database_privilege" in online


def test_compose_masks_migration_dsn_from_every_runtime_service() -> None:
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    runtime_database_lines = [
        index
        for index, line in enumerate(lines)
        if line.strip() == "DATABASE_URL: ${DATABASE_URL}"
    ]
    assert runtime_database_lines
    assert all(
        lines[index + 1].strip() == "MIGRATION_DATABASE_URL: ''"
        for index in runtime_database_lines
    )


def test_alembic_refuses_dotenv_migration_credential_before_app_import() -> None:
    source = (ROOT / "alembic/env.py").read_text(encoding="utf-8")
    guard = source.index('if "MIGRATION_DATABASE_URL" in dotenv_values(')
    assert source.index('os.environ.get("MIGRATION_DATABASE_URL"') < guard
    assert guard < source.index("from app.db import")
    assert "MIGRATION_DATABASE_URL must not be persisted in .env" in source


def test_host_make_migration_entrypoints_require_process_credential() -> None:
    source = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in (
        "migrate",
        "migrate-new",
        "migrate-down",
        "docker-migrate",
        "prod-migrate",
    ):
        assert f"{target}: migrate-env-check" in source
    assert "MIGRATION_DATABASE_URL is required" in source
    assert "MIGRATION_DATABASE_URL must not be in .env" in source


def test_shared_cluster_test_stack_requires_held_migration_login() -> None:
    source = (ROOT / "scripts/testing/test_stack.sh").read_text(encoding="utf-8")
    migration = source.split("run_migration_image() {", 1)[1].split("\n}", 1)[0]
    runtime = source.split("cmd_up() {", 1)[1].split("\n}", 1)[0]
    assert "MIGRATION_DATABASE_URL for dotmac_test is required" in migration
    assert '"app_admin", runtime.host' in migration
    assert 'command.upgrade(config, "heads")' in migration
    assert "ALTER ROLE" not in migration
    assert "bootstrap_disposable_database" not in migration
    assert 'runtime.username != "app_user"' in migration
    assert "-e PG_SUPER_PW" not in migration
    assert "-e MIGRATION_DATABASE_URL=" in runtime
    assert "PG_SUPER_PW" not in runtime
    assert "tail -6" not in source


def test_legacy_shadow_and_manual_repair_paths_refuse_migration() -> None:
    shadow = (ROOT / "deploy/shadow/docker-compose.shadow.yml").read_text(
        encoding="utf-8"
    )
    repair = (ROOT / ".github/workflows/temporary-module-prereq-repair.yml").read_text(
        encoding="utf-8"
    )
    assert "REFUSED: shadow migration" in shadow
    assert 'MIGRATION_DATABASE_URL: ""' in shadow
    assert 'MIGRATION_DATABASE_URL="$DATABASE_URL"' not in repair
    assert "REFUSED: this temporary workflow" in repair
