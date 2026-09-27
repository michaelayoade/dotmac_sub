"""Historical CRM ticket bindings remain visible but cannot be revived."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.integration import IntegrationJob, IntegrationRun, IntegrationTarget
from app.models.integration_platform import IntegrationInstallationState
from app.schemas.integration import IntegrationJobUpdate
from app.services import integration as integration_service
from app.services.integrations import installations
from app.services.integrations.registry import supported_connector_definitions
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.runtime_execution import (
    CapabilityUnavailableError,
    build_execution_context,
)


def _historical_ticket_binding(db):
    historical = next(
        definition
        for definition in supported_connector_definitions()
        if definition.key == "dotmac.crm" and definition.version == "1.3.0"
    )
    installation = installations.create_draft(
        db,
        connector_key="dotmac.crm",
        name=f"Historical CRM {uuid4()}",
        environment="test",
    )
    installation.connector_version = historical.version
    installation.manifest_digest = historical.digest
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={"base_url": "https://crm.example.test"},
        secret_refs={"service_credentials": "env://CRM_TEST_SERVICE_TOKEN"},
    )
    binding = installations.bind_capability(
        db,
        installation_id=installation.id,
        capability_id="crm.ticket_observation.v1",
    )
    return installation, binding


def test_historical_context_refuses_ticket_before_secret_resolution(db_session):
    _, binding = _historical_ticket_binding(db_session)
    resolved: list[str] = []

    with pytest.raises(CapabilityUnavailableError, match="retired"):
        build_execution_context(
            db_session,
            capability_binding_id=binding.id,
            allow_disabled=True,
            secret_resolver=lambda ref: resolved.append(str(ref)) or "test-material",
        )

    assert resolved == []


def test_connection_validation_and_direct_enable_keep_historical_ticket_disabled(
    db_session,
):
    installation, binding = _historical_ticket_binding(db_session)

    result = installations.validate_installation_connection(
        db_session, installation_id=installation.id
    )
    assert result.error_codes == ("retired_capability",)
    assert installation.state != IntegrationInstallationState.enabled.value
    assert binding.state == "disabled"

    with pytest.raises(installations.InstallationError, match="retired_capability"):
        installations.enable_after_connection_validation(
            db_session,
            installation_id=installation.id,
            connection_result=ValidationResult(valid=True),
        )
    assert installation.state != IntegrationInstallationState.enabled.value
    assert binding.state == "disabled"


def test_job_owner_refuses_ticket_before_run_creation_or_reactivation(db_session):
    _, binding = _historical_ticket_binding(db_session)
    target = IntegrationTarget(name=f"Historical CRM {uuid4()}")
    db_session.add(target)
    db_session.flush()
    job = IntegrationJob(
        target_id=target.id,
        name="Retired CRM ticket pull",
        capability_binding_id=binding.id,
        is_active=True,
    )
    db_session.add(job)
    db_session.flush()

    with pytest.raises(HTTPException) as run_error:
        integration_service.integration_jobs.run(db_session, str(job.id))
    assert run_error.value.status_code == 409
    assert db_session.query(IntegrationRun).filter_by(job_id=job.id).count() == 0

    with pytest.raises(HTTPException) as update_error:
        integration_service.integration_jobs.update(
            db_session, str(job.id), IntegrationJobUpdate(is_active=True)
        )
    assert update_error.value.status_code == 409
