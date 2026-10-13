"""Non-authoritative SQLite unit tests; deployed-schema proof belongs to CI."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.machine_models import MachineCredential
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationConfigRevision,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.services import deployment_secret_requirements as owner
from app.services.deployment_secret_schema import (
    DeploymentSecretSchemaResult,
    SchemaCapabilityState,
)
from app.services.integrations.registry import require_connector_definition
from app.services.operator_tenant import operator_tenant_id
from scripts.setup import verify_deployment_secret_requirements as cli

NOW = datetime(2026, 10, 13, tzinfo=UTC)
UNIT_SCHEMA = DeploymentSecretSchemaResult(
    integrations=SchemaCapabilityState.EVALUATED,
    machine_credentials=SchemaCapabilityState.EVALUATED,
    machine_attribution_available=True,
    authoritative=False,
)


@pytest.fixture
def requirement_db() -> Iterator[Session]:
    """Minimal metadata unit lane, never migration/production-parity evidence."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("tenants", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for model in (
        MachineCredential,
        IntegrationInstallation,
        IntegrationConfigRevision,
    ):
        model.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def _machine(
    db: Session,
    *,
    is_active: bool = True,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
    attributed: bool = True,
    tenant_id: UUID | None = None,
) -> None:
    tenant = tenant_id or operator_tenant_id()
    db.execute(sa.text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant.hex})
    db.add(
        MachineCredential(
            id=uuid4(),
            tenant_id=tenant,
            label="unit-readiness-credential",
            key_hash="hmac-sha256:" + "a" * 64,
            scopes=[],
            is_active=is_active,
            revoked_at=revoked_at,
            expires_at=expires_at,
            source_application="dotmac_erp" if attributed else None,
        )
    )
    db.flush()


def _installation(
    db: Session,
    *,
    enabled: bool = True,
    secret_refs: object | None = None,
    revision: bool = True,
) -> IntegrationInstallation:
    definition = require_connector_definition("whatsapp")
    installation = IntegrationInstallation(
        id=uuid4(),
        connector_key=definition.key,
        connector_version=definition.version,
        manifest_digest=definition.digest,
        name=f"sensitive-installation-label-{uuid4().hex}",
        state=(
            IntegrationInstallationState.enabled.value
            if enabled
            else IntegrationInstallationState.disabled.value
        ),
    )
    db.add(installation)
    db.flush()
    if revision:
        row = IntegrationConfigRevision(
            id=uuid4(),
            installation_id=installation.id,
            revision=1,
            config_json={},
            secret_refs=(
                {"service_credentials": "env://UNIT_DECLARED_BINDING"}
                if secret_refs is None
                else secret_refs
            ),
            config_digest="a" * 64,
        )
        db.add(row)
        db.flush()
        installation.current_config_revision_id = row.id
        db.flush()
    return installation


def _evaluate(db: Session) -> owner.DeploymentSecretRequirementsResult:
    return owner.evaluate_deployment_secret_requirements(
        db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=lambda _: None,
        binding_resolver=lambda _: "synthetic-unit-material",
        conversion_resolver=lambda: None,
    )


def _check(
    result: owner.DeploymentSecretRequirementsResult, name: owner.RequirementName
) -> owner.SecretRequirementCheck:
    return next(check for check in result.checks if check.name is name)


def test_empty_snapshot_reports_operation_only_and_dormant_material(
    requirement_db: Session,
) -> None:
    result = _evaluate(requirement_db)
    assert result.ok
    assert result.coverage is owner.RequirementCoverage.OPERATOR_SNAPSHOT
    assert (
        _check(result, owner.RequirementName.MACHINE_HMAC).requirement
        is owner.SecretRequirement.DORMANT
    )
    for name in (
        owner.RequirementName.PREPAID_ATTESTATION,
        owner.RequirementName.CONVERSION_INGEST,
    ):
        check = _check(result, name)
        assert check.requirement is owner.SecretRequirement.OPERATION_ONLY
        assert check.material is owner.MaterialStatus.ABSENT


@pytest.mark.parametrize(
    ("active", "revoked", "expiry", "attributed", "required"),
    [
        (True, None, None, True, True),
        (True, None, NOW + timedelta(seconds=1), True, True),
        (False, None, None, True, False),
        (True, NOW, None, True, False),
        (True, None, NOW, True, False),
        (True, None, NOW - timedelta(seconds=1), True, False),
        (True, None, None, False, False),
    ],
)
def test_machine_hmac_requirement_uses_kernel_lifecycle_and_attribution(
    requirement_db: Session,
    active: bool,
    revoked: datetime | None,
    expiry: datetime | None,
    attributed: bool,
    required: bool,
) -> None:
    _machine(
        requirement_db,
        is_active=active,
        revoked_at=revoked,
        expires_at=expiry,
        attributed=attributed,
    )
    result = _evaluate(requirement_db)
    check = _check(result, owner.RequirementName.MACHINE_HMAC)
    assert (
        check.requirement is owner.SecretRequirement.DEPLOYMENT_REQUIRED
    ) is required
    assert result.ok is not required


def test_machine_hmac_resolves_only_canonical_optional_path(
    requirement_db: Session,
) -> None:
    from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS

    _machine(requirement_db)
    seen: list[str] = []

    def resolve(reference: str) -> str | None:
        seen.append(reference)
        return (
            "synthetic-unit-material"
            if reference
            == OPTIONAL_SECRET_REFS[owner.RequirementName.MACHINE_HMAC.value]
            else None
        )

    result = owner.evaluate_deployment_secret_requirements(
        requirement_db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=resolve,
        conversion_resolver=lambda: None,
    )
    assert result.ok
    assert set(seen) == {
        OPTIONAL_SECRET_REFS[owner.RequirementName.MACHINE_HMAC.value],
        OPTIONAL_SECRET_REFS[owner.RequirementName.PREPAID_ATTESTATION.value],
    }


def test_schema_failure_is_unavailable_and_resolves_nothing() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    seen: list[str] = []
    with Session(engine) as db:
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
            optional_resolver=lambda reference: seen.append(reference),
            conversion_resolver=lambda: "should-not-be-read",
        )
    engine.dispose()
    assert not result.ok
    assert result == owner.unavailable_requirements(UNIT_SCHEMA)
    assert seen == []


def test_enabled_exact_pin_resolves_required_bindings_only(
    requirement_db: Session,
) -> None:
    _installation(
        requirement_db,
        secret_refs={
            "service_credentials": "env://UNIT_REQUIRED",
            "webhook_signing_secret": "env://UNIT_OPTIONAL",
        },
    )
    seen: list[str] = []

    def resolve(reference: str) -> str:
        seen.append(reference)
        return "synthetic-unit-material"

    result = owner.evaluate_deployment_secret_requirements(
        requirement_db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=lambda _: None,
        binding_resolver=resolve,
        conversion_resolver=lambda: None,
    )
    assert result.ok
    assert result.enabled_installation_count == 1
    assert seen == ["env://UNIT_REQUIRED"]
    assert "sensitive-installation-label" not in repr(result)
    assert "env://" not in repr(result)


def test_disabled_installation_unknown_pin_does_not_gate(
    requirement_db: Session,
) -> None:
    installation = _installation(requirement_db, enabled=False, revision=False)
    installation.manifest_digest = "b" * 64
    requirement_db.flush()
    result = _evaluate(requirement_db)
    assert result.ok
    assert result.enabled_installation_count == 0


@pytest.mark.parametrize("version", [False, True])
def test_enabled_unknown_manifest_pin_fails_closed(
    requirement_db: Session, version: bool
) -> None:
    installation = _installation(requirement_db)
    if version:
        installation.connector_version = "999.0.0"
    else:
        installation.manifest_digest = "b" * 64
    requirement_db.flush()
    result = _evaluate(requirement_db)
    assert not result.ok
    assert (
        _check(result, owner.RequirementName.INTEGRATION_PIN).material
        is owner.MaterialStatus.INVALID
    )


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_missing_or_foreign_current_revision_fails_closed(
    requirement_db: Session, wrong_owner: bool
) -> None:
    installation = _installation(requirement_db, revision=False)
    if wrong_owner:
        other = _installation(requirement_db)
        installation.current_config_revision_id = other.current_config_revision_id
        requirement_db.flush()
    result = _evaluate(requirement_db)
    assert not result.ok
    assert (
        _check(result, owner.RequirementName.INTEGRATION_REVISION).material
        is owner.MaterialStatus.INVALID
    )


@pytest.mark.parametrize(
    "refs",
    [
        [],
        ["plaintext"],
        {"service_credentials": []},
        {"service_credentials": "plaintext"},
        {"unknown": "env://UNIT_UNKNOWN"},
        {},
    ],
)
def test_malformed_or_missing_required_binding_fails_closed(
    requirement_db: Session, refs: object
) -> None:
    _installation(requirement_db, secret_refs=refs)
    result = _evaluate(requirement_db)
    assert not result.ok
    assert _check(result, owner.RequirementName.INTEGRATION_BINDINGS).material in (
        owner.MaterialStatus.INVALID,
        owner.MaterialStatus.ABSENT,
    )


@pytest.mark.parametrize("exception", [False, True])
def test_binding_resolution_missing_or_failure_is_not_ready(
    requirement_db: Session, exception: bool
) -> None:
    _installation(requirement_db)

    def resolve(_: str) -> str | None:
        if exception:
            raise RuntimeError("sensitive-unit-payload")
        return None

    result = owner.evaluate_deployment_secret_requirements(
        requirement_db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=lambda _: None,
        binding_resolver=resolve,
        conversion_resolver=lambda: None,
    )
    assert not result.ok
    assert "sensitive-unit-payload" not in repr(result)


def test_optional_transport_failure_gates_even_dormant_features(
    requirement_db: Session,
) -> None:
    def unavailable(_: str) -> str:
        raise RuntimeError("sensitive-unit-payload")

    result = owner.evaluate_deployment_secret_requirements(
        requirement_db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=unavailable,
        conversion_resolver=lambda: None,
    )
    assert not result.ok
    assert all(
        check.material is owner.MaterialStatus.UNAVAILABLE
        for check in result.checks
        if check.name is not owner.RequirementName.CONVERSION_INGEST
    )


@pytest.mark.parametrize("configured", [False, True])
def test_conversion_preserves_setting_then_held_then_optional_precedence(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    from dotmac_kernel import secret_sources

    from app import config

    monkeypatch.setattr(
        config,
        "settings",
        replace(
            config.settings,
            conversion_ingest_api_key="configured-unit-material" if configured else "",
        ),
    )
    seen: list[str] = []

    def held(name: str) -> str:
        seen.append(name)
        return "held-unit-material"

    monkeypatch.setattr(secret_sources, "get_secret", held)
    monkeypatch.setattr(
        owner,
        "resolve_openbao_ref_optional",
        lambda _: pytest.fail("must preserve precedence"),
    )
    assert owner._conversion_material() == (
        "configured-unit-material" if configured else "held-unit-material"
    )
    assert len(seen) == (0 if configured else 1)


def test_conversion_absent_held_source_uses_canonical_optional_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dotmac_kernel import secret_sources

    from app import config
    from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS

    monkeypatch.setattr(
        config, "settings", replace(config.settings, conversion_ingest_api_key="")
    )
    monkeypatch.setattr(secret_sources, "get_secret", lambda _: None)
    seen: list[str] = []

    def optional(reference: str) -> str:
        seen.append(reference)
        return "optional-unit-material"

    monkeypatch.setattr(owner, "resolve_openbao_ref_optional", optional)
    assert owner._conversion_material() == "optional-unit-material"
    assert seen == [OPTIONAL_SECRET_REFS[owner.RequirementName.CONVERSION_INGEST.value]]


def test_query_rejects_naive_time() -> None:
    with pytest.raises(owner.DeploymentSecretRequirementsError, match="timezone-aware"):
        owner.DeploymentSecretRequirementsQuery(NOW.replace(tzinfo=None), UNIT_SCHEMA)


def test_cli_snapshot_error_is_generic_redacted_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def unavailable() -> Iterator[Session]:
        logging.error("sensitive-log-payload")
        raise RuntimeError("sensitive-error-payload")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "read_only_snapshot_session", unavailable)
    previous = logging.root.manager.disable
    assert cli.main(["--component", "app"]) == 1
    output = capsys.readouterr()
    assert "evaluation_unavailable" in output.out
    assert "sensitive-" not in output.out + output.err
    assert logging.root.manager.disable == previous


def test_query_aggregate_receipt_is_bounded_and_does_not_flush_pending_state(
    requirement_db: Session,
) -> None:
    _installation(requirement_db)
    _installation(requirement_db)
    requirement_db.add(
        IntegrationInstallation(
            id=uuid4(),
            connector_key="unknown",
            connector_version="0.0.0",
            manifest_digest="a" * 64,
            name="not-flushed",
            state="enabled",
        )
    )
    result = _evaluate(requirement_db)
    assert result.ok
    assert result.enabled_installation_count == 2
    assert _check(result, owner.RequirementName.INTEGRATION_PIN).count == 2
    assert len(requirement_db.new) == 1
    assert len(result.checks) <= 6


def test_cli_delegates_query_inside_snapshot_and_prints_safe_success(
    requirement_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered: list[bool] = []

    @contextmanager
    def snapshot() -> Iterator[Session]:
        entered.append(True)
        yield requirement_db
        entered.append(False)

    def evaluate(
        db: Session, query: owner.DeploymentSecretRequirementsQuery
    ) -> owner.DeploymentSecretRequirementsResult:
        assert entered == [True]
        assert db is requirement_db
        assert query.evaluated_at.tzinfo is not None
        return _evaluate(db)

    def assess(
        db: Session, *, query: cli.DeploymentSecretSchemaQuery
    ) -> DeploymentSecretSchemaResult:
        assert entered == [True]
        assert db is requirement_db
        return UNIT_SCHEMA

    monkeypatch.setattr(cli, "evaluate_deployment_secret_schema", assess)
    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "evaluate_deployment_secret_requirements", evaluate)
    assert cli.main(["--component", "app"]) == 0
    output = capsys.readouterr().out
    assert "Declared secret requirements satisfied." in output
    assert "operator_tenant_snapshot" in output
    assert "all features" not in output
    assert entered == [True, False]


@pytest.mark.parametrize(
    "state",
    [
        SchemaCapabilityState.NOT_INSTALLED_AT_PREDECESSOR,
        SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP,
    ],
)
def test_known_not_installed_schema_is_explicit_and_never_queried(
    state: SchemaCapabilityState,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    schema = DeploymentSecretSchemaResult(
        integrations=state,
        machine_credentials=state,
        machine_attribution_available=False,
        authoritative=False,
    )
    with Session(engine) as db:
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(NOW, schema),
            optional_resolver=lambda _: None,
            conversion_resolver=lambda: None,
        )
    engine.dispose()
    assert result.ok
    assert result.schema.integrations is state
    assert result.schema.machine_credentials is state
    assert all(
        check.name is not owner.RequirementName.MACHINE_HMAC for check in result.checks
    )


def test_unavailable_assessment_never_reads_features_or_material() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    schema = DeploymentSecretSchemaResult(
        integrations=SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        machine_credentials=SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        machine_attribution_available=False,
        authoritative=False,
    )
    with Session(engine) as db:
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(NOW, schema),
            optional_resolver=lambda _: pytest.fail("material must not be read"),
            conversion_resolver=lambda: pytest.fail("material must not be read"),
        )
    engine.dispose()
    assert not result.ok
    assert result == owner.unavailable_requirements(schema)


@pytest.mark.parametrize("active", [False, True])
def test_pre_attribution_machine_schema_reads_only_lifecycle_columns(
    active: bool,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    machine = sa.Table(
        "machine_credentials",
        metadata,
        sa.Column("tenant_id", sa.Uuid),
        sa.Column("is_active", sa.Boolean),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(engine)
    schema = DeploymentSecretSchemaResult(
        integrations=SchemaCapabilityState.NOT_INSTALLED_AT_PREDECESSOR,
        machine_credentials=SchemaCapabilityState.EVALUATED,
        machine_attribution_available=False,
        authoritative=False,
    )
    with Session(engine) as db:
        db.execute(
            sa.insert(machine).values(is_active=active, tenant_id=operator_tenant_id())
        )
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(NOW, schema),
            optional_resolver=lambda _: None,
            conversion_resolver=lambda: None,
        )
    engine.dispose()
    assert result.ok is not active
    check = _check(result, owner.RequirementName.MACHINE_HMAC)
    assert check.requirement is (
        owner.SecretRequirement.DEPLOYMENT_REQUIRED
        if active
        else owner.SecretRequirement.DORMANT
    )


def test_integration_predecessor_query_reads_only_introduction_columns() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    installations = sa.Table(
        "integration_installations",
        metadata,
        sa.Column("id", sa.Uuid),
        sa.Column("state", sa.String),
        sa.Column("connector_key", sa.String),
        sa.Column("connector_version", sa.String),
        sa.Column("manifest_digest", sa.String),
        sa.Column("current_config_revision_id", sa.Uuid),
    )
    revisions = sa.Table(
        "integration_config_revisions",
        metadata,
        sa.Column("id", sa.Uuid),
        sa.Column("installation_id", sa.Uuid),
        sa.Column("secret_refs", sa.JSON),
    )
    metadata.create_all(engine)
    definition = require_connector_definition("whatsapp")
    installation_id, revision_id = uuid4(), uuid4()
    schema = DeploymentSecretSchemaResult(
        integrations=SchemaCapabilityState.EVALUATED,
        machine_credentials=SchemaCapabilityState.NOT_INSTALLED_AT_PREDECESSOR,
        machine_attribution_available=False,
        authoritative=False,
    )
    with Session(engine) as db:
        db.execute(
            sa.insert(installations).values(
                id=installation_id,
                state="enabled",
                connector_key=definition.key,
                connector_version=definition.version,
                manifest_digest=definition.digest,
                current_config_revision_id=revision_id,
            )
        )
        db.execute(
            sa.insert(revisions).values(
                id=revision_id,
                installation_id=installation_id,
                secret_refs={"service_credentials": "env://UNIT_REQUIRED"},
            )
        )
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(NOW, schema),
            optional_resolver=lambda _: None,
            binding_resolver=lambda _: "unit-material",
            conversion_resolver=lambda: None,
        )
    engine.dispose()
    assert result.ok
    assert result.enabled_installation_count == 1


@pytest.mark.parametrize("attribution", [False, True])
def test_postmigration_requires_all_installed_schema_capabilities(
    attribution: bool,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    schema = DeploymentSecretSchemaResult(
        integrations=SchemaCapabilityState.EVALUATED
        if not attribution
        else SchemaCapabilityState.NOT_INSTALLED_AT_PREDECESSOR,
        machine_credentials=SchemaCapabilityState.EVALUATED,
        machine_attribution_available=attribution,
        authoritative=False,
    )
    with Session(engine) as db:
        result = owner.evaluate_deployment_secret_requirements(
            db,
            owner.DeploymentSecretRequirementsQuery(
                NOW, schema, owner.RequirementEvaluationPhase.POST_MIGRATION
            ),
            optional_resolver=lambda _: pytest.fail("must refuse before resolution"),
            conversion_resolver=lambda: pytest.fail("must refuse before resolution"),
        )
    engine.dispose()
    assert not result.ok
    assert result == owner.unavailable_requirements(schema)


def test_cli_threads_typed_bootstrap_evidence_and_postmigration_phase(
    requirement_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    authorization_path = tmp_path / "bootstrap-authorization.json"
    seen_phases: list[owner.RequirementEvaluationPhase] = []

    @contextmanager
    def snapshot() -> Iterator[Session]:
        yield requirement_db

    def assess(
        db: Session, *, query: cli.DeploymentSecretSchemaQuery
    ) -> DeploymentSecretSchemaResult:
        assert db is requirement_db
        assert query.bootstrap_authorization_path == authorization_path
        assert query.candidate_revision == cli.GitCommitSha("a" * 40)
        assert query.target_server == cli.ProductionServerName("dotmac-sub-prod")
        return UNIT_SCHEMA

    def evaluate(
        db: Session, query: owner.DeploymentSecretRequirementsQuery
    ) -> owner.DeploymentSecretRequirementsResult:
        assert query.schema is UNIT_SCHEMA
        seen_phases.append(query.phase)
        return _evaluate(db)

    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "evaluate_deployment_secret_schema", assess)
    monkeypatch.setattr(cli, "evaluate_deployment_secret_requirements", evaluate)
    assert (
        cli.main(
            [
                "--bootstrap-authorization",
                str(authorization_path),
                "--candidate-revision",
                "a" * 40,
                "--target-server",
                "dotmac-sub-prod",
                "--require-installed-schema",
            ]
        )
        == 0
    )
    assert seen_phases == [owner.RequirementEvaluationPhase.POST_MIGRATION]
    output = capsys.readouterr().out
    assert "integrations=evaluated" in output
    assert "machine_credentials=evaluated" in output
    assert str(authorization_path) not in output


def test_machine_scope_excludes_foreign_tenant_on_non_rls_backend(
    requirement_db: Session,
) -> None:
    foreign_tenant = uuid4()
    _machine(requirement_db, tenant_id=foreign_tenant)
    # SQLite has no RLS, so this canary proves the explicit owner predicate.
    assert (
        requirement_db.scalar(sa.select(sa.func.count()).select_from(MachineCredential))
        == 1
    )
    assert (
        _check(
            _evaluate(requirement_db), owner.RequirementName.MACHINE_HMAC
        ).requirement
        is owner.SecretRequirement.DORMANT
    )
    _machine(requirement_db)
    assert (
        _check(
            _evaluate(requirement_db), owner.RequirementName.MACHINE_HMAC
        ).requirement
        is owner.SecretRequirement.DEPLOYMENT_REQUIRED
    )


@pytest.mark.parametrize("surface", ["machine", "binding", "conversion"])
def test_whitespace_material_is_invalid(requirement_db: Session, surface: str) -> None:
    if surface == "machine":
        _machine(requirement_db)
    if surface == "binding":
        _installation(requirement_db)
    result = owner.evaluate_deployment_secret_requirements(
        requirement_db,
        owner.DeploymentSecretRequirementsQuery(NOW, UNIT_SCHEMA),
        optional_resolver=lambda _: " \t\n" if surface == "machine" else None,
        binding_resolver=lambda _: " \t\n",
        conversion_resolver=lambda: " \t\n" if surface == "conversion" else None,
    )
    name = {
        "machine": owner.RequirementName.MACHINE_HMAC,
        "binding": owner.RequirementName.INTEGRATION_BINDINGS,
        "conversion": owner.RequirementName.CONVERSION_INGEST,
    }[surface]
    assert _check(result, name).material is owner.MaterialStatus.INVALID
    assert not result.ok
