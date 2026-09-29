"""Retired CRM ticket capability cannot execute through historical pins."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
    DotmacCrmRunner,
)
from app.services.integrations.registry import (
    connector_definition,
    pinned_connector_definition,
    supported_connector_definitions,
)
from app.services.integrations.runtime import (
    CapabilityValidationRunner,
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
)
from app.services.integrations.runtime_execution import validate_connection


class _CrmTransportSpy:
    def __init__(self) -> None:
        self.subscriber_reads = 0

    def list_subscribers(self, **_kwargs):
        self.subscriber_reads += 1
        return []


def _ticket_envelope(manifest):
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="retired-crm-ticket",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
        connector_key="dotmac.crm",
        connector_version=manifest.version,
        manifest_digest=manifest.digest,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.manual,
        idempotency_key="retired-crm-ticket",
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        payload={"action": "list_tickets", "params": {}},
    )


def test_current_manifest_retires_ticket_capability_and_preserves_old_pin() -> None:
    current = connector_definition("dotmac.crm")
    assert current is not None
    assert current.version == "1.4.0"
    assert current.capability(CRM_TICKET_OBSERVATION_CAPABILITY) is None

    prior = next(
        definition
        for definition in supported_connector_definitions()
        if definition.key == "dotmac.crm" and definition.version == "1.3.0"
    )
    assert prior.capability(CRM_TICKET_OBSERVATION_CAPABILITY) is not None
    assert (
        pinned_connector_definition(
            "dotmac.crm", version=prior.version, manifest_digest=prior.digest
        )
        is prior
    )


def test_historical_ticket_binding_is_rejected_without_transport_call(
    monkeypatch,
) -> None:
    historical = next(
        definition
        for definition in supported_connector_definitions()
        if definition.key == "dotmac.crm" and definition.version == "1.3.0"
    )
    spy = _CrmTransportSpy()
    runner = DotmacCrmRunner(client_override=spy)
    assert runner.supports_capability(CRM_TICKET_OBSERVATION_CAPABILITY) is False
    # The tombstone must win even if a future action-map edit adds a ticket
    # action. Historical manifest pins can identify the ID but cannot run it.
    from app.services.integrations.connectors import dotmac_crm

    monkeypatch.setitem(
        dotmac_crm._ACTIONS_BY_CAPABILITY,
        CRM_TICKET_OBSERVATION_CAPABILITY,
        {"list_tickets"},
    )
    assert runner.supports_capability(CRM_TICKET_OBSERVATION_CAPABILITY) is False

    assert isinstance(runner, CapabilityValidationRunner)
    validation = validate_connection(
        SimpleNamespace(
            binding=SimpleNamespace(capability_id=CRM_TICKET_OBSERVATION_CAPABILITY),
            manifest=historical,
            config={"base_url": "https://crm.example.test"},
            secret_material={},
            runner=runner,
        )
    )

    assert validation.valid is False
    assert validation.error_codes == ("retired_capability",)
    for action in ("list_tickets", "get_ticket", "list_ticket_comments"):
        operation = runner.execute(
            _ticket_envelope(historical).model_copy(
                update={"payload": {"action": action, "params": {}}}
            ),
            config={"base_url": "https://crm.example.test"},
            secret_material={},
        )
        assert operation.status == OperationStatus.rejected
        assert operation.error_code == "capability_not_supported"
    assert spy.subscriber_reads == 0


def test_retained_crm_capability_validates_through_subscriber_access() -> None:
    current = connector_definition("dotmac.crm")
    assert current is not None
    spy = _CrmTransportSpy()
    runner = DotmacCrmRunner(client_override=spy)

    result = runner.validate_capability(
        capability_id="crm.subscriber_observation.v1",
        manifest=current,
        config={"base_url": "https://crm.example.test"},
        secret_material={},
    )

    assert result.valid is True
    assert spy.subscriber_reads == 1
