"""Authoritative catalogue and same-connection snapshot checks on real migrations."""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from app import db as database
from app.services import deployment_secret_schema as owner
from scripts.ci import template_database
from scripts.release_artifact_contract import (
    GitCommitSha,
    ProductionBootstrapAuthorization,
    ProductionServerName,
)
from scripts.release_candidate_evidence import write_bootstrap_authorization


def test_migrated_schema_capabilities_on_read_only_snapshot(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert engine.dialect.name == "postgresql"
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, autoflush=False)
    )
    with database.read_only_snapshot_session() as db:
        connection = db.connection()
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
        assert db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        result = owner.evaluate_deployment_secret_schema(
            db, query=owner.DeploymentSecretSchemaQuery()
        )
        assert db.connection() is connection
        assert result.authoritative and result.ok
        assert result.integrations is owner.SchemaCapabilityState.EVALUATED
        assert result.machine_credentials is owner.SchemaCapabilityState.EVALUATED
        assert result.machine_attribution_available
        assert not db.new and not db.dirty and not db.deleted


def test_extension_owned_relations_excluded_by_actual_catalogue_membership(
    db_session: Session,
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    extension_relations = frozenset(
        db_session.execute(
            text(
                """
                SELECT n.nspname, c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_depend d ON d.objid = c.oid
                  AND d.classid = 'pg_catalog.pg_class'::regclass
                  AND d.refclassid = 'pg_catalog.pg_extension'::regclass
                  AND d.deptype = 'e'
                WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                """
            )
        ).tuples()
    )
    # The real PostGIS migration chain must provide extension relations;
    # absence is a broken integration target rather than skipped evidence.
    assert extension_relations
    application_relations = frozenset(
        (schema, name)
        for schema, name, _kind in db_session.execute(text(owner._RELATIONS_SQL))
    )
    assert not extension_relations & application_relations
    assert ("public", "integration_installations") in application_relations
    assert ("public", "machine_credentials") in application_relations


@pytest.mark.parametrize("table", sorted(owner._REQUIREMENT_RELATIONS))
def test_temporary_relation_shadow_refuses_schema_capability(
    db_session: Session,
    table: str,
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    assert table in owner._REQUIREMENT_RELATIONS
    connection = db_session.connection()
    before = owner.evaluate_deployment_secret_schema(
        db_session, query=owner.DeploymentSecretSchemaQuery()
    )
    assert before.ok
    # Static parameterization above owns these SQL identifiers. A temporary
    # empty table must never redirect the subsequent feature population reads.
    db_session.execute(
        text(f"CREATE TEMP TABLE {table} (shadow_marker integer) ON COMMIT DROP")
    )
    db_session.execute(text("SET LOCAL search_path TO pg_temp, pg_catalog, public"))
    result = owner.evaluate_deployment_secret_schema(
        db_session, query=owner.DeploymentSecretSchemaQuery()
    )
    assert db_session.connection() is connection
    assert not result.ok
    assert result.integrations is owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE
    assert (
        result.machine_credentials is owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE
    )


@pytest.fixture
def empty_bootstrap_database(template_base_url: URL) -> Iterator[Engine]:
    """Own a genuinely empty database, without cloning the migrated head."""
    name = "dotmac_secret_bootstrap_test_" + uuid4().hex
    maintenance = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        maintenance.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    target = template_base_url.set(drivername="postgresql+psycopg", database=name)
    try:
        engine = create_engine(target)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        template_database.drop_database(template_base_url, name)


def _bootstrap_authority(
    tmp_path: Path,
) -> tuple[Path, GitCommitSha, ProductionServerName]:
    path = tmp_path / "bootstrap.json"
    revision = GitCommitSha("a" * 40)
    server = ProductionServerName("dotmac-sub-prod")
    write_bootstrap_authorization(
        path,
        ProductionBootstrapAuthorization(
            revision, server, "CHG-123", "first bootstrap"
        ),
    )
    return path, revision, server


class _BootstrapAuthorizationMode(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    STALE = "stale"


@pytest.mark.parametrize(
    ("setup_sql", "authorization_mode", "expected"),
    [
        (
            (),
            _BootstrapAuthorizationMode.MISSING,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
        (
            (),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP,
        ),
        (
            (
                "CREATE TABLE public.alembic_version (version_num varchar(255) NOT NULL)",
            ),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP,
        ),
        (
            (),
            _BootstrapAuthorizationMode.STALE,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
        (
            ("CREATE TYPE public.bootstrap_marker AS ENUM ('present')",),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
        (
            (
                "CREATE FUNCTION public.bootstrap_marker() RETURNS integer "
                "LANGUAGE sql IMMUTABLE AS 'SELECT 1'",
            ),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
        (
            ("CREATE TYPE public.alembic_version AS ENUM ('unrelated_type')",),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
        (
            (
                "CREATE TABLE public.alembic_version (version_num varchar(255) NOT NULL)",
                "CREATE DOMAIN public.bootstrap_marker AS integer",
            ),
            _BootstrapAuthorizationMode.VALID,
            owner.SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        ),
    ],
)
def test_fresh_database_bootstrap_catalogue_authority(
    empty_bootstrap_database: Engine,
    tmp_path: Path,
    setup_sql: tuple[str, ...],
    authorization_mode: _BootstrapAuthorizationMode,
    expected: owner.SchemaCapabilityState,
) -> None:
    with empty_bootstrap_database.begin() as connection:
        for statement in setup_sql:
            connection.execute(text(statement))
    path, revision, server = _bootstrap_authority(tmp_path)
    query = (
        owner.DeploymentSecretSchemaQuery()
        if authorization_mode is _BootstrapAuthorizationMode.MISSING
        else owner.DeploymentSecretSchemaQuery(
            path,
            GitCommitSha("b" * 40)
            if authorization_mode is _BootstrapAuthorizationMode.STALE
            else revision,
            server,
        )
    )
    with Session(empty_bootstrap_database) as db:
        database.begin_read_only_snapshot(db)
        result = owner.evaluate_deployment_secret_schema(db, query=query)
        assert result.integrations is expected
        assert result.machine_credentials is expected
        assert result.ok is (
            expected is owner.SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP
        )
        assert not result.machine_attribution_available
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
