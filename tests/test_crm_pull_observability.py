"""Scheduled CRM pull observability: run history, change-only records."""

from __future__ import annotations

from uuid import uuid4

from app.models.integration import IntegrationRecord, IntegrationRun
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.services import integration as integration_service
from app.services.integration_sync import run_scheduled_pull
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from tests.test_crm_ticket_pull import FakeCrmClient, _crm_ticket


def _crm_job(db):
    installation = installations.create_draft(
        db,
        connector_key="dotmac.crm",
        name=f"CRM {uuid4().hex[:6]}",
        environment="test",
    )
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={"base_url": "https://crm.example.test", "timeout_seconds": 45},
        secret_refs={"service_credentials": "env://CRM_TEST_TOKEN"},
    )
    binding = installations.bind_capability(
        db,
        installation_id=installation.id,
        capability_id="crm.ticket_observation.v1",
        policy={"default": True},
    )
    installations.validate_static(db, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    target = integration_service.integration_targets.create(
        db,
        IntegrationTargetCreate(
            name=f"CRM Target {uuid4().hex[:6]}",
            target_type="custom",
        ),
    )
    return integration_service.integration_jobs.create(
        db,
        IntegrationJobCreate(
            target_id=target.id,
            name=f"CRM Ticket Pull {uuid4().hex[:6]}",
            job_type="sync",
            schedule_type="manual",
            capability_binding_id=binding.id,
        ),
    )


def test_scheduled_pull_records_run_and_changes_only(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    job = _crm_job(db_session)
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[_crm_ticket(crm_ticket_id, "50001", "2026-06-10T01:00:00Z")],
        subscribers={},
        comments={crm_ticket_id: []},
    )

    first = run_scheduled_pull(db_session, client=client, full=True)
    second = run_scheduled_pull(db_session, client=client, full=True)

    assert first["created"] == 1
    assert second["unchanged"] == 1

    runs = (
        db_session.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .order_by(IntegrationRun.started_at)
        .all()
    )
    assert len(runs) == 2
    assert all(r.status.value == "success" for r in runs)
    assert runs[0].trigger == "scheduled_full"
    assert runs[0].metrics["created"] == 1
    assert second["mode"] == "full"

    records = (
        db_session.query(IntegrationRecord)
        .filter(IntegrationRecord.run_id.in_([r.id for r in runs]))
        .all()
    )
    # one record for the create; the unchanged second pass records nothing
    assert len(records) == 1
    assert records[0].action == "created"


def test_scheduled_pull_incremental_trigger_label(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    job = _crm_job(db_session)
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[_crm_ticket(crm_ticket_id, "50003", "2026-06-10T01:00:00Z")],
        subscribers={},
        comments={crm_ticket_id: []},
    )

    run_scheduled_pull(db_session, client=client, full=True)  # seed watermark
    metrics = run_scheduled_pull(db_session, client=client, full=False)

    assert metrics["mode"] == "incremental"
    latest = (
        db_session.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .order_by(IntegrationRun.started_at.desc())
        .first()
    )
    assert latest.trigger == "scheduled"


def test_scheduled_pull_without_job_fails_closed(db_session, subscriber):
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    client = FakeCrmClient(
        tickets=[_crm_ticket(str(uuid4()), "50002", "2026-06-10T01:00:00Z")],
        subscribers={},
        comments={},
    )

    import pytest

    from app.services.integration_sync import SyncAdapterError

    with pytest.raises(SyncAdapterError, match="no active CRM"):
        run_scheduled_pull(db_session, client=client, full=True)
    assert db_session.query(IntegrationRun).count() == 0
