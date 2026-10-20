"""Refusals of the dedicated production migration identity (``dotmac_sub_migrator``).

No database is touched. ``alembic/env.py`` is executed with inert metadata and
a scripted connection so every refusal is exercised through the real adapter:

- production mode never falls back to the runtime ``DATABASE_URL``;
- the connected role must be the migrator, BYPASSRLS, and not a superuser;
- ownership is handed back to ``dotmac_app`` in the same transaction and the
  upgrade is refused (so it rolls back) if anything stays migrator-owned.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy
import sqlalchemy.dialects.postgresql
from alembic.config import Config

import alembic
from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    MIGRATOR_MEMBERSHIPS,
    MIGRATOR_ROLE,
    RUNTIME_OWNER_ROLE,
    MigratorObservation,
    migration_identity_violations,
    migrator_violations,
)
from app.config import Settings

RUNTIME_URL = "postgresql+psycopg://runtime@db.example.test/sub"
MIGRATOR_URL = f"postgresql+psycopg://{MIGRATOR_ROLE}@db.example.test/sub"
ENV_PATH = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


# --- Settings: no silent fallback -------------------------------------------


def test_production_mode_refuses_the_runtime_url_fallback() -> None:
    settings = Settings(
        database_url=RUNTIME_URL,
        migration_database_url=None,
        migration_production_mode=True,
        migration_schema_owner=RUNTIME_OWNER_ROLE,
    )
    with pytest.raises(ValueError, match="requires MIGRATION_DATABASE_URL") as error:
        _ = settings.alembic_database_url
    assert RUNTIME_URL not in str(error.value)


def test_production_mode_requires_a_schema_owner() -> None:
    settings = Settings(
        database_url=RUNTIME_URL,
        migration_database_url=MIGRATOR_URL,
        migration_production_mode=True,
        migration_schema_owner=None,
    )
    with pytest.raises(ValueError, match="requires MIGRATION_SCHEMA_OWNER"):
        _ = settings.alembic_database_url


@pytest.mark.parametrize("owner", ["", " ", "\t\n"])
def test_a_supplied_blank_schema_owner_refuses(owner: str) -> None:
    settings = Settings(
        database_url=RUNTIME_URL,
        migration_database_url=MIGRATOR_URL,
        migration_schema_owner=owner,
    )
    with pytest.raises(ValueError, match="MIGRATION_SCHEMA_OWNER must not be blank"):
        _ = settings.alembic_database_url


def test_production_mode_with_complete_material_selects_the_migrator() -> None:
    settings = Settings(
        database_url=RUNTIME_URL,
        migration_database_url=MIGRATOR_URL,
        migration_production_mode=True,
        migration_schema_owner=RUNTIME_OWNER_ROLE,
    )
    assert settings.alembic_database_url == MIGRATOR_URL
    assert settings.database_url == RUNTIME_URL


def test_the_legacy_path_is_unchanged_outside_production_mode() -> None:
    settings = Settings(database_url=RUNTIME_URL, migration_database_url=None)
    assert settings.alembic_database_url == RUNTIME_URL


# --- Pure posture contract ---------------------------------------------------


def test_the_migrator_is_in_the_role_contract_with_the_exact_posture() -> None:
    assert COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[MIGRATOR_ROLE].posture == (
        True,  # LOGIN
        True,  # BYPASSRLS
        False,  # not a superuser
    )
    # The runtime owner must stay unable to bypass RLS.
    assert COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[RUNTIME_OWNER_ROLE].posture == (
        True,
        False,
        False,
    )


def _good() -> MigratorObservation:
    return MigratorObservation(
        createdb=False,
        createrole=False,
        replication=False,
        inherit=True,
        can_connect=True,
        memberships=dict(MIGRATOR_MEMBERSHIPS),
        members=(),
    )


def test_a_contract_migrator_has_no_violations() -> None:
    assert migrator_violations(_good()) == ()
    # A missing role is reported by the posture contract, not here.
    assert migrator_violations(None) == ()


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"createdb": True}, "NOCREATEDB"),
        ({"createrole": True}, "NOCREATEROLE"),
        ({"replication": True}, "NOREPLICATION"),
        ({"inherit": False}, "must be INHERIT"),
        ({"can_connect": False}, "lacks CONNECT"),
        (
            {"memberships": {"app_admin": (True, True, False)}},
            "not a member of dotmac_app",
        ),
        (
            {"memberships": {RUNTIME_OWNER_ROLE: (True, True, False)}},
            "not a member of app_admin",
        ),
        (
            {"memberships": {**MIGRATOR_MEMBERSHIPS, "app_user": (True, True, False)}},
            "outside the contract",
        ),
        (
            {
                "memberships": {
                    **MIGRATOR_MEMBERSHIPS,
                    RUNTIME_OWNER_ROLE: (True, True, True),
                }
            },
            "membership in dotmac_app",
        ),
        ({"members": (RUNTIME_OWNER_ROLE,)}, "is a member of dotmac_sub_migrator"),
    ],
)
def test_migrator_drift_is_refused(change: dict[str, Any], fragment: str) -> None:
    observed = MigratorObservation(**{**_good().__dict__, **change})
    violations = migrator_violations(observed)
    assert any(fragment in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        (
            {"current_user": MIGRATOR_ROLE, "superuser": True, "bypass_rls": True},
            "superuser",
        ),
        (
            {"current_user": MIGRATOR_ROLE, "superuser": False, "bypass_rls": False},
            "lacks BYPASSRLS",
        ),
        (
            {
                "current_user": RUNTIME_OWNER_ROLE,
                "superuser": False,
                "bypass_rls": True,
            },
            "must connect as",
        ),
    ],
)
def test_the_connected_identity_is_refused_when_wrong(
    kwargs: dict[str, Any], fragment: str
) -> None:
    violations = migration_identity_violations(production_mode=True, **kwargs)
    assert any(fragment in violation for violation in violations), violations


def test_the_expected_identity_is_accepted() -> None:
    assert (
        migration_identity_violations(
            current_user=MIGRATOR_ROLE,
            superuser=False,
            bypass_rls=True,
            production_mode=True,
        )
        == ()
    )


# --- alembic/env.py through the real adapter ---------------------------------


class ScriptedConnection:
    """Answers the adapter's catalog queries; records every statement."""

    def __init__(
        self,
        *,
        current_user: str,
        superuser: bool = False,
        bypass_rls: bool = True,
        residue: dict[str, int] | None = None,
        dialect: str = "postgresql",
    ) -> None:
        self.dialect = SimpleNamespace(
            name=dialect,
            identifier_preparer=sqlalchemy.dialects.postgresql.dialect().identifier_preparer,
        )
        self._row = (current_user, superuser, bypass_rls)
        self._residue = residue or {}
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        result = MagicMock()
        if "FROM pg_catalog.pg_roles AS r" in statement:
            result.one.return_value = self._row
        elif statement.startswith("SELECT count(*)"):
            label = {
                "pg_class": "relations",
                "pg_proc": "functions",
                "pg_type": "types",
                "pg_namespace": "schemas",
            }
            for catalog, name in label.items():
                if catalog in statement:
                    result.scalar_one.return_value = self._residue.get(name, 0)
        return result

    def execute(self, *args: Any, **kwargs: Any) -> MagicMock:
        return MagicMock()

    def commit(self) -> None:
        self.statements.append("COMMIT")


def _load_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    migration_url: str | None,
    owner: str | None,
    production: bool,
    connection: ScriptedConnection | None = None,
) -> tuple[dict[str, Any], MagicMock, Callable[[], None]]:
    """Execute alembic/env.py online with a scripted connection."""
    config = Config()
    context = MagicMock()
    context.config = config
    context.is_offline_mode.return_value = False
    monkeypatch.setattr(alembic, "context", context)
    monkeypatch.setitem(sys.modules, "alembic.context", context)
    monkeypatch.setattr(
        "app.config.settings",
        Settings(
            database_url=RUNTIME_URL,
            migration_database_url=migration_url,
            migration_schema_owner=owner,
            migration_production_mode=production,
        ),
    )
    for name in (
        "dotmac_kernel.planes",
        "dotmac_kernel.prerequisites",
        "app.migration_bindings",
        "app.migration_schema_ops",
        "app.models",
    ):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    db = ModuleType("app.db")
    db.Base = SimpleNamespace(metadata=sqlalchemy.MetaData())  # type: ignore[attr-defined]
    db.apply_migration_idle_transaction_timeout = MagicMock()  # type: ignore[attr-defined]
    db.resolve_migration_lock_timeout = MagicMock(return_value="5s")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", db)

    connection = connection or ScriptedConnection(current_user=MIGRATOR_ROLE)
    engine_factory = MagicMock()
    engine_factory.return_value.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(sqlalchemy, "engine_from_config", engine_factory)
    monkeypatch.setattr(sqlalchemy, "Table", MagicMock())
    return {}, context, lambda: runpy.run_path(str(ENV_PATH))


def _run(monkeypatch: pytest.MonkeyPatch, connection: ScriptedConnection, **kw: Any):
    _, context, execute = _load_env(monkeypatch, connection=connection, **kw)
    execute()
    return context


PROD = {"migration_url": MIGRATOR_URL, "owner": RUNTIME_OWNER_ROLE, "production": True}


def test_the_migrator_upgrades_then_hands_everything_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=MIGRATOR_ROLE)
    context = _run(monkeypatch, connection, **PROD)

    context.run_migrations.assert_called_once_with()
    assert any(
        statement == "REASSIGN OWNED BY CURRENT_USER TO dotmac_app"
        for statement in connection.statements
    ), connection.statements
    # The residual-ownership proof ran for relations, functions, types, schemas.
    probes = [s for s in connection.statements if s.startswith("SELECT count(*)")]
    assert len(probes) == 4


def test_leftover_migrator_objects_refuse_the_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(
        current_user=MIGRATOR_ROLE, residue={"functions": 2}
    )
    with pytest.raises(
        RuntimeError, match="still owned by the migration role.*functions=2"
    ):
        _run(monkeypatch, connection, **PROD)


@pytest.mark.parametrize(
    ("connection", "fragment"),
    [
        (
            ScriptedConnection(current_user=MIGRATOR_ROLE, superuser=True),
            "superuser",
        ),
        (
            ScriptedConnection(current_user=MIGRATOR_ROLE, bypass_rls=False),
            "lacks BYPASSRLS",
        ),
        (
            ScriptedConnection(current_user=RUNTIME_OWNER_ROLE, bypass_rls=True),
            "must connect as 'dotmac_sub_migrator'",
        ),
        (
            ScriptedConnection(current_user=RUNTIME_OWNER_ROLE, bypass_rls=False),
            "lacks BYPASSRLS",
        ),
    ],
)
def test_production_mode_refuses_a_wrong_connected_identity(
    monkeypatch: pytest.MonkeyPatch, connection: ScriptedConnection, fragment: str
) -> None:
    with pytest.raises(RuntimeError, match="migration identity refused") as error:
        _run(monkeypatch, connection, **PROD)
    assert fragment in str(error.value)
    # Refused before any migration or ownership statement ran.
    assert not any("REASSIGN" in s for s in connection.statements)


def test_production_mode_requires_the_runtime_owner_as_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=MIGRATOR_ROLE)
    with pytest.raises(RuntimeError, match="MIGRATION_SCHEMA_OWNER=dotmac_app"):
        _run(
            monkeypatch,
            connection,
            migration_url=MIGRATOR_URL,
            owner="someone_else",
            production=True,
        )


def test_production_mode_refuses_a_non_postgres_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=MIGRATOR_ROLE, dialect="sqlite")
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        _run(monkeypatch, connection, **PROD)


def test_production_mode_without_migration_url_never_reaches_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=RUNTIME_OWNER_ROLE)
    with pytest.raises(ValueError, match="requires MIGRATION_DATABASE_URL"):
        _run(
            monkeypatch,
            connection,
            migration_url=None,
            owner=RUNTIME_OWNER_ROLE,
            production=True,
        )
    assert connection.statements == []


def test_a_non_bypass_non_owner_migrator_is_refused_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting an owner alone opts in to the posture assertion."""
    connection = ScriptedConnection(current_user="some_login", bypass_rls=False)
    with pytest.raises(RuntimeError, match="lacks BYPASSRLS"):
        _run(
            monkeypatch,
            connection,
            migration_url=MIGRATOR_URL,
            owner=RUNTIME_OWNER_ROLE,
            production=False,
        )


def test_the_owner_migrating_its_own_objects_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=RUNTIME_OWNER_ROLE, bypass_rls=False)
    _run(
        monkeypatch,
        connection,
        migration_url=RUNTIME_URL,
        owner=RUNTIME_OWNER_ROLE,
        production=False,
    )
    assert not any("REASSIGN" in s for s in connection.statements)


def test_the_legacy_path_asserts_and_reassigns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(current_user=RUNTIME_OWNER_ROLE, bypass_rls=False)
    _run(
        monkeypatch,
        connection,
        migration_url=None,
        owner=None,
        production=False,
    )
    assert not any("pg_roles" in s or "REASSIGN" in s for s in connection.statements)


# --- Provisioning: the bootstrap brings the migrator to contract -------------


class FakeProvisioningConnection:
    """Serves ``observe_migrator`` and records every mutation."""

    def __init__(
        self,
        *,
        memberships: dict[str, tuple[bool, bool, bool]],
        members: list[str],
        createrole: bool = False,
    ) -> None:
        self._memberships = memberships
        self._members = members
        self._createrole = createrole
        self.mutations: list[str] = []

    def execute(self, query: Any, params: Any = None) -> MagicMock:
        text_query = query if isinstance(query, str) else query.as_string(None)
        result = MagicMock()
        if "FROM pg_roles WHERE rolname = %s" in text_query:
            result.fetchone.return_value = (False, self._createrole, False, True, True)
        elif "WHERE r.rolname = %s" in text_query:
            result.fetchall.return_value = [
                (role, *options) for role, options in self._memberships.items()
            ]
        elif "WHERE g.rolname = %s" in text_query:
            result.fetchall.return_value = [(member,) for member in self._members]
        elif "current_database()" in text_query:
            result.fetchone.return_value = ("sub",)
        else:
            self.mutations.append(text_query)
        return result


def test_provisioning_repairs_a_drifted_migrator_without_setting_a_password() -> None:
    from scripts.bootstrap_commercial_module_prereqs import _repair_migrator

    conn = FakeProvisioningConnection(
        memberships={
            RUNTIME_OWNER_ROLE: (True, False, False),
            "app_user": (True, True, False),
        },
        members=[RUNTIME_OWNER_ROLE],
        createrole=True,
    )
    _repair_migrator(conn, dry_run=False)  # type: ignore[arg-type]

    joined = "\n".join(conn.mutations)
    grant_options = "WITH INHERIT TRUE, SET TRUE, ADMIN FALSE"
    assert (
        'ALTER ROLE "dotmac_sub_migrator" NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT'
        in joined
    )
    assert f'GRANT "app_admin" TO "dotmac_sub_migrator" {grant_options}' in joined
    # A membership with the wrong options is revoked and re-granted exactly.
    assert 'REVOKE "dotmac_app" FROM "dotmac_sub_migrator"' in joined
    assert f'GRANT "dotmac_app" TO "dotmac_sub_migrator" {grant_options}' in joined
    # Anything outside the contract is removed, in both directions.
    assert 'REVOKE "app_user" FROM "dotmac_sub_migrator"' in joined
    assert 'REVOKE "dotmac_sub_migrator" FROM "dotmac_app"' in joined
    assert "ADMIN TRUE" not in joined
    assert "PASSWORD" not in joined.upper()


def test_provisioning_dry_run_mutates_nothing() -> None:
    from scripts.bootstrap_commercial_module_prereqs import _repair_migrator

    conn = FakeProvisioningConnection(memberships={}, members=[])
    _repair_migrator(conn, dry_run=True)  # type: ignore[arg-type]
    assert conn.mutations == []
