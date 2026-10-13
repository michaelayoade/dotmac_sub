"""Migrated PostgreSQL requirements and read-only snapshot proofs (CI lane)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.machine_models import MachineCredential
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import db as database
from app.services import deployment_secret_requirements as owner
from app.services.deployment_secret_schema import (
    DeploymentSecretSchemaQuery,
    evaluate_deployment_secret_schema,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from scripts.ci.migrated_test_database import repository_heads

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _evaluate(db: Session) -> owner.DeploymentSecretRequirementsResult:
    return owner.evaluate_deployment_secret_requirements(
        db,
        owner.DeploymentSecretRequirementsQuery(
            NOW,
            evaluate_deployment_secret_schema(db, query=DeploymentSecretSchemaQuery()),
        ),
        optional_resolver=lambda _: None,
        binding_resolver=lambda _: "synthetic-ci-material",
        conversion_resolver=lambda: None,
    )


def _machine_check(db: Session) -> owner.SecretRequirementCheck:
    return next(
        check
        for check in _evaluate(db).checks
        if check.name is owner.RequirementName.MACHINE_HMAC
    )


def test_migrated_machine_admission_requires_material(db_session: Session) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    assert (
        frozenset(
            db_session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
        == repository_heads()
    )
    row = MachineCredential(
        id=uuid4(),
        tenant_id=OPERATOR_TENANT_ID,
        label=f"ci-readiness-{uuid4().hex}",
        key_hash="hmac-sha256:" + uuid4().hex + uuid4().hex,
        scopes=[],
        is_active=True,
        source_application="dotmac_erp",
    )
    db_session.add(row)
    db_session.flush()
    check = _machine_check(db_session)
    assert check.requirement is owner.SecretRequirement.DEPLOYMENT_REQUIRED
    assert check.material is owner.MaterialStatus.ABSENT
    assert not check.ok
    row.revoked_at = NOW
    db_session.flush()
    assert _machine_check(db_session).requirement is owner.SecretRequirement.DORMANT


def test_owner_runs_in_repository_read_only_snapshot(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert engine.dialect.name == "postgresql"
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, autoflush=False)
    )
    with database.read_only_snapshot_session() as db:
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
        assert db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(
            OPERATOR_TENANT_ID
        )
        result = _evaluate(db)
        assert all(
            check.requirement is not owner.SecretRequirement.EVALUATION_UNAVAILABLE
            for check in result.checks
        )
        assert not db.new and not db.dirty and not db.deleted
        assert db.scalar(text("SHOW transaction_read_only")) == "on"


def test_operator_machine_scope_with_visible_foreign_tenant_row(
    db_session: Session,
) -> None:
    # The migrated fixture's provisioning role is deliberately broad. Prove
    # the foreign canary is visible before relying on the explicit predicate.
    posture = db_session.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    assert posture[0] or posture[1]
    foreign_tenant, foreign_credential = uuid4(), uuid4()
    db_session.execute(
        text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, :slug, 'CI foreign canary', true)"
        ),
        {"id": str(foreign_tenant), "slug": f"ci-readiness-{foreign_tenant.hex}"},
    )
    db_session.add(
        MachineCredential(
            id=foreign_credential,
            tenant_id=foreign_tenant,
            label=f"ci-foreign-{uuid4().hex}",
            key_hash="hmac-sha256:" + uuid4().hex + uuid4().hex,
            scopes=[],
            is_active=True,
            source_application="dotmac_erp",
        )
    )
    db_session.flush()
    assert (
        db_session.scalar(
            select(MachineCredential.id).where(
                MachineCredential.id == foreign_credential
            )
        )
        == foreign_credential
    )
    assert _machine_check(db_session).requirement is owner.SecretRequirement.DORMANT
