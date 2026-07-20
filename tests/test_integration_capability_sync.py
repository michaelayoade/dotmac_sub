from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.integration import IntegrationRun
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCheckpoint,
    IntegrationInstallationState,
)
from app.services import integration_sync
from app.services.integrations import installations
from app.services.integrations.connectors.dotmac_crm import DotmacCrmRunner
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)
from tests.test_crm_pull_observability import _crm_job
from tests.test_crm_ticket_pull import FakeCrmClient, _crm_ticket


def _fake_client(ticket_id: str) -> FakeCrmClient:
    return FakeCrmClient(
        tickets=[_crm_ticket(ticket_id, "59001", "2026-07-20T01:00:00Z")],
        subscribers={},
        comments={ticket_id: []},
    )


def test_crm_job_runs_only_through_pinned_capability(db_session, subscriber) -> None:
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    job = _crm_job(db_session)
    metrics = integration_sync.run_scheduled_pull(
        db_session,
        client=_fake_client(str(uuid4())),
        full=True,
    )

    db_session.refresh(job)
    binding = job.capability_binding
    installation = binding.installation
    revision = installation.current_config_revision
    run = db_session.query(IntegrationRun).filter(IntegrationRun.job_id == job.id).one()
    checkpoint = (
        db_session.query(IntegrationCheckpoint)
        .filter(IntegrationCheckpoint.job_id == job.id)
        .one()
    )

    assert binding.capability_id == "crm.ticket_observation.v1"
    assert binding.state == IntegrationBindingState.enabled.value
    assert installation.state == IntegrationInstallationState.enabled.value
    assert revision.secret_refs == {"service_credentials": "env://CRM_TEST_TOKEN"}
    assert "service_token" not in revision.config_json
    assert metrics["created"] == 1
    assert run.capability_binding_id == binding.id
    assert run.config_revision_id == revision.id
    assert run.connector_key == "dotmac.crm"
    assert run.manifest_digest == installation.manifest_digest
    assert checkpoint.last_run_id == run.id
    assert checkpoint.cursor_json["watermark"].startswith("2026-07-20T01:00:00")


def test_capability_sync_fails_closed_until_connection_validation(
    db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 24296
    db_session.commit()
    job = _crm_job(db_session)
    ticket_id = str(uuid4())
    client = _fake_client(ticket_id)
    binding = job.capability_binding
    installations.disable_installation(
        db_session,
        installation_id=binding.installation_id,
        reason="test connection gate",
    )
    with pytest.raises(Exception, match="not enabled"):
        integration_sync.run_scheduled_pull(
            db_session,
            client=client,
            full=True,
        )
    context = build_execution_context(
        db_session,
        capability_binding_id=binding.id,
        allow_disabled=True,
        runner_override=DotmacCrmRunner(client),
        secret_resolver=lambda _reference: "test-service-credential",
    )
    connection_result = validate_connection(context)
    assert connection_result.valid
    installations.enable_after_connection_validation(
        db_session,
        installation_id=binding.installation_id,
        connection_result=connection_result,
        actor="test-operator",
    )
    db_session.commit()

    capability = integration_sync.run_scheduled_pull(
        db_session,
        client=client,
        full=True,
    )

    assert capability["created"] == 1
    assert binding.installation.state == IntegrationInstallationState.enabled.value
    assert binding.state == IntegrationBindingState.enabled.value


def test_capability_sync_migration_is_linear_and_contains_no_secret_material() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/377_integration_capability_sync.py"
    )
    spec = importlib.util.spec_from_file_location("migration_373", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "377_integration_capability_sync"
    assert module.down_revision == "376_integration_platform_foundation"
    source = path.read_text(encoding="utf-8")
    assert "integration_checkpoints" in source
    assert "capability_binding_id" in source
    assert "manifest_digest" in source
    assert "service_token" not in source


def test_sync_dispatcher_has_no_hard_coded_crm_action_branch() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app/services/integration_sync.py"
    ).read_text(encoding="utf-8")

    assert 'if adapter_key == "crm"' not in source
    assert "_SYNC_CAPABILITY_HANDLERS" in source
    assert "_LEGACY_CAPABILITY_MIGRATORS" not in source
    assert "CRMClient" not in source
    assert "shadow_parity" not in source
