"""Pure identity comparison for the two deployment database logins."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.commercial_module_prereqs import MigrationPrincipalObservation
from scripts import verify_database_connection_pair as pair

REPO = Path(__file__).resolve().parents[1]


def _observations() -> tuple[pair.ConnectionObservation, pair.ConnectionObservation]:
    backend = pair.BackendIdentity(
        database="dotmac_sub",
        address="172.20.255.2",
        port=5432,
        postmaster_started_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    runtime = pair.ConnectionObservation(
        principal=MigrationPrincipalObservation(
            session_user="app_user",
            current_user="app_user",
            can_login=True,
            bypass_rls=False,
            superuser=False,
            can_create_database=False,
            can_create_role=False,
            database_create=False,
        ),
        backend=backend,
    )
    migration = pair.ConnectionObservation(
        principal=MigrationPrincipalObservation(
            session_user="app_admin",
            current_user="app_admin",
            can_login=True,
            bypass_rls=True,
            superuser=False,
            can_create_database=False,
            can_create_role=False,
            database_create=False,
        ),
        backend=backend,
    )
    return runtime, migration


def test_distinct_correct_actual_logins_on_same_backend_pass() -> None:
    runtime, migration = _observations()
    assert pair.pair_refusal(runtime, migration) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("database", "other_database"),
        ("address", "172.20.255.3"),
        ("port", 5433),
        (
            "postmaster_started_at",
            datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=1),
        ),
    ],
)
def test_any_backend_difference_blocks(field: str, value: object) -> None:
    runtime, migration = _observations()
    migration = replace(migration, backend=replace(migration.backend, **{field: value}))
    assert pair.pair_refusal(runtime, migration) == "backend_mismatch"


def test_role_drift_and_set_role_masquerade_block() -> None:
    runtime, migration = _observations()
    assert (
        pair.pair_refusal(
            replace(
                runtime, principal=replace(runtime.principal, current_user="app_admin")
            ),
            migration,
        )
        == "runtime_principal"
    )
    assert (
        pair.pair_refusal(
            replace(runtime, principal=replace(runtime.principal, bypass_rls=True)),
            migration,
        )
        == "runtime_principal"
    )
    assert (
        pair.pair_refusal(
            replace(
                runtime, principal=replace(runtime.principal, database_create=True)
            ),
            migration,
        )
        == "runtime_principal"
    )
    assert (
        pair.pair_refusal(
            runtime,
            replace(
                migration,
                principal=replace(migration.principal, current_user="app_user"),
            ),
        )
        == "migration_principal"
    )
    assert (
        pair.pair_refusal(
            runtime,
            replace(
                migration, principal=replace(migration.principal, database_create=True)
            ),
        )
        == "migration_principal"
    )
    assert pair.pair_refusal(None, migration) == "incomplete_observation"


def test_incomplete_non_tcp_or_malformed_observation_blocks() -> None:
    runtime, migration = _observations()
    row = (
        "app_user",
        "app_user",
        True,
        False,
        False,
        False,
        False,
        False,
        runtime.backend.database,
        runtime.backend.address,
        runtime.backend.port,
        runtime.backend.postmaster_started_at,
    )
    assert pair.decode_observation(row) == runtime
    assert pair.decode_observation(row[:11]) is None
    assert pair.decode_observation((*row[:9], None, *row[10:])) is None
    assert pair.decode_observation((*row[:10], None, row[11])) is None
    assert pair.decode_observation((*row[:11], datetime(2026, 10, 1))) is None
    with pytest.raises(ValueError, match="incomplete backend"):
        replace(migration.backend, address="")


def test_catalog_normalizes_ipv4_and_ipv6_without_accepting_cidr_identity() -> None:
    runtime, _migration = _observations()
    assert "host(inet_server_addr())" in pair._CATALOG_SQL
    assert "inet_server_addr()::text" not in pair._CATALOG_SQL
    base = (
        "app_user",
        "app_user",
        True,
        False,
        False,
        False,
        False,
        False,
        runtime.backend.database,
    )
    tail = (runtime.backend.port, runtime.backend.postmaster_started_at)
    for address in ("172.17.0.2/32", "2001:db8::1/128", "not-an-ip"):
        assert pair.decode_observation((*base, address, *tail)) is None
    assert pair.decode_observation((*base, "172.17.0.2", *tail)) is not None
    assert pair.decode_observation((*base, "2001:db8::1", *tail)) is not None


def test_main_reports_generic_error_without_connection_or_url_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_url = "postgresql://app_user:synthetic@db/dotmac_sub"
    migration_url = "postgresql://app_admin:synthetic@db/dotmac_sub"
    monkeypatch.setenv("DATABASE_PAIR_RUNTIME_URL", runtime_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)

    def failure(_url: str) -> pair.ConnectionObservation:
        raise RuntimeError("synthetic private connection details")

    monkeypatch.setattr(pair, "observe_connection", failure)
    assert pair.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "database pair refused: connection_or_catalog_error\n"
    assert runtime_url not in output.err and migration_url not in output.err


def test_deploy_checks_pair_before_bootstrap_and_backup_without_url_argv() -> None:
    source = (REPO / "scripts/deploy.sh").read_text()
    preflight = source.split("migration_connection_preflight() {", 1)[1].split(
        "\n}", 1
    )[0]
    assert 'DATABASE_PAIR_RUNTIME_URL="${runtime_url}"' in preflight
    assert "-e DATABASE_PAIR_RUNTIME_URL -e MIGRATION_DATABASE_URL" in preflight
    assert "python scripts/verify_database_connection_pair.py" in preflight
    assert preflight.index("verify_database_connection_pair.py") > preflight.index(
        "env_value DATABASE_URL"
    )
    execution = source.split("\nmigration_connection_preflight\n", 1)[1]
    assert execution.index("run_database_prerequisite_bootstrap") < execution.index(
        'log "Backing up database before migrations"'
    )
    assert execution.index("run_database_prerequisite_bootstrap") < execution.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )
