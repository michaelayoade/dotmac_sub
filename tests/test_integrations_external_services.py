import uuid

from app.models.connector import ConnectorAuthType, ConnectorType
from app.models.external import ExternalEntityType
from app.schemas.connector import ConnectorConfigCreate
from app.schemas.external import ExternalReferenceCreate, ExternalReferenceSync
from app.schemas.integration import (
    IntegrationJobCreate,
    IntegrationTargetCreate,
)
from app.services import connector as connector_service
from app.services import external as external_service
from app.services import integration as integration_service


def test_integration_job_run(db_session):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="TestConnector",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
        ),
    )
    target = integration_service.integration_targets.create(
        db_session,
        IntegrationTargetCreate(
            name="Radius Sync",
            target_type="radius",
            connector_config_id=connector.id,
        ),
    )
    job = integration_service.integration_jobs.create(
        db_session,
        IntegrationJobCreate(
            target_id=target.id,
            name="Nightly Sync",
            job_type="sync",
            schedule_type="manual",
        ),
    )
    run = integration_service.integration_jobs.run(db_session, str(job.id))
    assert run.status.value == "success"


def test_external_reference_sync(db_session):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="ExternalCRM",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
        ),
    )
    entity_id = uuid.uuid4()
    created = external_service.external_references.create(
        db_session,
        ExternalReferenceCreate(
            connector_config_id=connector.id,
            entity_type=ExternalEntityType.ticket,
            entity_id=entity_id,
            external_id="TCK-123",
        ),
    )
    synced = external_service.sync_reference(
        db_session,
        ExternalReferenceSync(
            connector_config_id=connector.id,
            entity_type=ExternalEntityType.ticket,
            entity_id=entity_id,
            external_id="TCK-123",
            metadata={"status": "synced"},
        ),
    )
    assert synced.last_synced_at is not None
    assert synced.metadata_["status"] == "synced"
