"""Connection-selection unit tests; no database or migrations are executed."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy
from alembic.config import Config

import alembic
from app.config import Settings

RUNTIME_URL = "postgresql+psycopg://runtime@localhost/runtime_test"
OWNER_URL = "postgresql+psycopg://owner:encoded%40value%25@localhost/owner_test"
ENV_PATH = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def test_owner_url_wins_without_changing_runtime_authority() -> None:
    settings = Settings(database_url=RUNTIME_URL, migration_database_url=OWNER_URL)

    assert settings.alembic_database_url == OWNER_URL
    assert settings.database_url == RUNTIME_URL
    assert OWNER_URL not in repr(settings)


def test_only_absent_owner_material_uses_runtime_fallback() -> None:
    settings = Settings(database_url=RUNTIME_URL, migration_database_url=None)

    assert settings.alembic_database_url == RUNTIME_URL


@pytest.mark.parametrize("owner_url", ["", " ", "\t\n"])
def test_supplied_blank_owner_material_refuses_with_sanitized_error(
    owner_url: str,
) -> None:
    settings = Settings(database_url=RUNTIME_URL, migration_database_url=owner_url)

    with pytest.raises(ValueError) as error:
        _ = settings.alembic_database_url

    assert str(error.value) == "MIGRATION_DATABASE_URL must not be blank when supplied"
    assert RUNTIME_URL not in str(error.value)


@pytest.mark.parametrize("owner_url", [OWNER_URL, None, "", " \t\n"])
def test_initialized_settings_read_owner_material_from_environment(
    tmp_path: Path, owner_url: str | None
) -> None:
    # A fresh interpreter exercises class defaults and the actual settings
    # singleton. Supply only synthetic environment values, and prevent dotenv
    # from discovering a host/repository credential file in this unit test.
    child_env = {
        "PYTHONPATH": str(ENV_PATH.parents[1]),
        "DATABASE_URL": RUNTIME_URL,
    }
    if owner_url is not None:
        child_env["MIGRATION_DATABASE_URL"] = owner_url
    script = """
import os
import sys
import dotenv

dotenv.load_dotenv = lambda: None

try:
    from app.config import settings

    runtime = os.environ["DATABASE_URL"]
    owner = os.environ.get("MIGRATION_DATABASE_URL")
    assert settings.database_url == runtime
    assert settings.migration_database_url == owner
    if owner is not None and not owner.strip():
        try:
            settings.alembic_database_url
        except ValueError as error:
            assert str(error) == "MIGRATION_DATABASE_URL must not be blank when supplied"
        else:
            raise AssertionError
    else:
        assert settings.alembic_database_url == (runtime if owner is None else owner)
    assert settings.database_url == runtime
except Exception:
    print("failed")
    sys.exit(1)
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=child_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""


def _run_env(
    monkeypatch: pytest.MonkeyPatch, *, offline: bool, owner_url: str | None
) -> tuple[Config, MagicMock, MagicMock]:
    """Run the actual adapter with inert metadata/kernel/connection boundaries."""
    config = Config()
    context = MagicMock()
    context.config = config
    context.is_offline_mode.return_value = offline
    monkeypatch.setattr(alembic, "context", context)
    monkeypatch.setitem(sys.modules, "alembic.context", context)
    monkeypatch.setattr(
        "app.config.settings",
        Settings(database_url=RUNTIME_URL, migration_database_url=owner_url),
    )

    for module_name in (
        "dotmac_kernel.planes",
        "dotmac_kernel.prerequisites",
        "app.migration_bindings",
        "app.migration_schema_ops",
        "app.models",
    ):
        monkeypatch.setitem(sys.modules, module_name, MagicMock())

    db = ModuleType("app.db")
    db.Base = SimpleNamespace(metadata=sqlalchemy.MetaData())  # type: ignore[attr-defined]
    db.apply_migration_idle_transaction_timeout = MagicMock()  # type: ignore[attr-defined]
    db.resolve_migration_lock_timeout = MagicMock(return_value="5s")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", db)

    engine_factory = MagicMock()
    connection = engine_factory.return_value.connect.return_value.__enter__.return_value
    connection.dialect.name = "sqlite"
    monkeypatch.setattr(sqlalchemy, "engine_from_config", engine_factory)
    monkeypatch.setattr(sqlalchemy, "Table", MagicMock())

    runpy.run_path(str(ENV_PATH))
    return config, context, engine_factory


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("owner_url", [OWNER_URL, None])
def test_online_and_offline_consume_selected_url_without_losing_percent_encoding(
    monkeypatch: pytest.MonkeyPatch, offline: bool, owner_url: str | None
) -> None:
    config, context, engine_factory = _run_env(
        monkeypatch, offline=offline, owner_url=owner_url
    )
    expected = RUNTIME_URL if owner_url is None else owner_url

    assert config.get_main_option("sqlalchemy.url") == expected
    if offline:
        assert context.configure.call_args.kwargs["url"] == expected
        engine_factory.assert_not_called()
    else:
        assert engine_factory.call_args.args[0]["sqlalchemy.url"] == expected
        assert "connection" in context.configure.call_args.kwargs
    context.run_migrations.assert_called_once_with()


@pytest.mark.parametrize("offline", [False, True])
def test_blank_owner_refuses_adapter_setup(
    monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    with pytest.raises(ValueError, match="^MIGRATION_DATABASE_URL must not be blank"):
        _run_env(monkeypatch, offline=offline, owner_url=" \t")


@pytest.mark.parametrize("fail", [False, True])
def test_test_migration_owner_is_scoped_and_runtime_is_preserved(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from app import config as app_config
    from scripts.ci import migrated_test_database as migration

    original = Settings(database_url=RUNTIME_URL, migration_database_url=OWNER_URL)
    monkeypatch.setattr(app_config, "settings", original)
    target = migration.parse_test_database_target(
        "postgresql+psycopg://postgres:synthetic@localhost/disposable_test"
    )

    def upgrade(config: Config, revision: str) -> None:
        assert revision == "heads"
        assert app_config.settings.alembic_database_url == target.url.render_as_string(
            hide_password=False
        )
        assert app_config.settings.database_url == RUNTIME_URL
        if fail:
            raise RuntimeError("synthetic migration refusal")

    monkeypatch.setattr(migration.command, "upgrade", upgrade)
    engine = MagicMock()
    monkeypatch.setattr(migration, "create_engine", MagicMock(return_value=engine))
    state = migration.MigratedSchemaState(
        actual_heads=frozenset({"test"}), expected_heads=frozenset({"test"})
    )
    monkeypatch.setattr(migration, "require_migrated_schema", lambda _: state)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic migration refusal"):
            migration.migrate_test_database(target)
    else:
        assert migration.migrate_test_database(target) is state
        engine.dispose.assert_called_once_with()
    assert app_config.settings is original


@pytest.mark.parametrize("fail", [False, True])
def test_template_owner_targets_its_disposable_database_and_restores_settings(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from app import config as app_config
    from scripts.ci.template_database import _alembic_target

    original = Settings(database_url=RUNTIME_URL, migration_database_url=OWNER_URL)
    monkeypatch.setattr(app_config, "settings", original)
    target = sqlalchemy.make_url(
        "postgresql+psycopg://postgres:synthetic@localhost/template_test"
    )
    try:
        with _alembic_target(target):
            assert app_config.settings.alembic_database_url == target.render_as_string(
                hide_password=False
            )
            if fail:
                raise RuntimeError("synthetic migration refusal")
    except RuntimeError:
        assert fail
    assert app_config.settings is original
