"""Explicit connector definition registry and compatibility projections."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass

from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    DataAccessManifest,
    EgressManifest,
    HealthManifest,
    RuntimeManifest,
    SecretBindingManifest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectorRegistryEntry:
    """Compatibility projection consumed by the current marketplace UI."""

    key: str
    name: str
    version: str
    connector_type: str
    description: str
    module_name: str
    file_size_bytes: int


_DEFINITIONS: tuple[ConnectorManifest, ...] = (
    ConnectorManifest(
        key="lead.capture.http",
        name="Lead Capture Webhook",
        version="1.0.0",
        connector_type="sales",
        description=(
            "Provider-neutral signed ingress for canonical lead-capture payloads."
        ),
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.lead_capture_http",
        ),
        capabilities=(
            CapabilityManifest(
                id="sales.lead_capture.v1",
                modes=(CapabilityMode.inbound,),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "signature_header": {"type": "string", "minLength": 1},
                "delivery_id_header": {"type": "string", "minLength": 1},
                "signature_prefix": {"type": "string"},
            },
            "required": [
                "signature_header",
                "delivery_id_header",
                "signature_prefix",
            ],
            "additionalProperties": False,
        },
        secrets=(SecretBindingManifest(name="webhook_signing_secret"),),
        data_access=DataAccessManifest(
            emits=("sales.lead_capture_observation",),
            classifications=("customer_contact", "marketing_attribution"),
        ),
        egress=EgressManifest(),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="webhook.http",
        name="HTTP Webhook",
        version="1.0.0",
        connector_type="automation",
        description="Approved outbound HTTPS event delivery transport.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.http_webhook",
        ),
        capabilities=(
            CapabilityManifest(
                id="events.deliver.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_attempts": {"type": "integer"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="signing_secret", required=False),
            SecretBindingManifest(name="authorization", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("events.outbound_projection",),
            emits=("events.external_delivery_receipt",),
            classifications=("domain_event_projection",),
        ),
        egress=EgressManifest(allow_installation_hosts=True),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="dotmac.crm",
        name="DotMac CRM",
        version="1.0.0",
        connector_type="crm",
        description="First-party CRM observations, commands, sessions, and inbound events.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.dotmac_crm",
        ),
        capabilities=(
            CapabilityManifest(
                id="crm.subscriber_observation.v1",
                modes=(
                    CapabilityMode.scheduled,
                    CapabilityMode.manual,
                    CapabilityMode.reconcile,
                ),
            ),
            CapabilityManifest(
                id="crm.ticket_observation.v1",
                modes=(
                    CapabilityMode.scheduled,
                    CapabilityMode.manual,
                    CapabilityMode.reconcile,
                ),
            ),
            CapabilityManifest(
                id="crm.operational_observation.v1",
                modes=(
                    CapabilityMode.scheduled,
                    CapabilityMode.interactive,
                    CapabilityMode.reconcile,
                ),
            ),
            CapabilityManifest(
                id="crm.portal_session.v1",
                modes=(CapabilityMode.interactive,),
            ),
            CapabilityManifest(
                id="crm.quote_command.v1",
                modes=(CapabilityMode.interactive,),
            ),
            CapabilityManifest(
                id="crm.events.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "public_portal_api_base": {"type": "string"},
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="service_credentials"),
            SecretBindingManifest(name="webhook_signing_secret", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("subscriber.external_identity", "portal.command_request"),
            emits=("crm.external_observation", "crm.inbound_event_observation"),
            classifications=("customer_contact", "support_content", "operations"),
        ),
        egress=EgressManifest(hosts=("crm.dotmac.io",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="whatsapp",
        name="WhatsApp",
        version="1.0.0",
        connector_type="messaging",
        description="Template and notification messaging connector.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.whatsapp_runtime",
        ),
        capabilities=(
            CapabilityManifest(
                id="messaging.send.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="messaging.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="messaging.templates.read.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["meta_cloud_api"]},
                "phone_number": {"type": "string"},
                "waba_id": {"type": "string"},
                "webhook_url": {"type": "string"},
                "graph_version": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "templates": {"type": "array"},
            },
            "required": ["provider"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="service_credentials"),
            SecretBindingManifest(name="webhook_signing_secret", required=False),
            SecretBindingManifest(name="webhook_verify_token", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("communications.outbound_message",),
            emits=("communications.inbound_message_observation",),
            classifications=("customer_contact", "message_content"),
        ),
        egress=EgressManifest(hosts=("graph.facebook.com",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="dotmac.erp",
        name="DotMac ERP",
        version="1.0.0",
        connector_type="erp",
        description="First-party ERP transport and observation connector.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.dotmac_erp",
        ),
        capabilities=(
            CapabilityManifest(
                id="erp.outbox.deliver.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="erp.status.read.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
            ),
            CapabilityManifest(
                id="erp.inventory.read.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.manual),
            ),
            CapabilityManifest(
                id="erp.operational_context.sync.v1",
                modes=(CapabilityMode.scheduled,),
            ),
            CapabilityManifest(
                id="erp.regulatory.read.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "max_retries": {"type": "integer"},
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(SecretBindingManifest(name="service_credentials"),),
        data_access=DataAccessManifest(
            reads=(
                "field.erp_outbox",
                "operations.context_projection",
                "inventory.query",
                "regulatory.query",
            ),
            emits=("erp.transport_observation",),
            classifications=("financial", "operations", "inventory"),
        ),
        egress=EgressManifest(hosts=("erp.dotmac.io",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="paystack",
        name="Paystack",
        version="1.0.0",
        connector_type="payment",
        description="Online payment gateway integration.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.payment_gateway",
        ),
        capabilities=(
            CapabilityManifest(
                id="payments.intent.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="payments.webhook.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="payments.reconcile.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
            ),
            CapabilityManifest(
                id="payments.refund.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "default_currency": {"type": "string"},
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="gateway_credentials"),
            SecretBindingManifest(name="public_key", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("financial.payment_intent",),
            emits=("financial.payment_provider_observation",),
            classifications=("financial", "customer_contact"),
        ),
        egress=EgressManifest(hosts=("api.paystack.co",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="flutterwave",
        name="Flutterwave",
        version="1.0.0",
        connector_type="payment",
        description="Online payment gateway integration.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.payment_gateway",
        ),
        capabilities=(
            CapabilityManifest(
                id="payments.intent.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="payments.webhook.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="payments.reconcile.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
            ),
            CapabilityManifest(
                id="payments.refund.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "default_currency": {"type": "string"},
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="gateway_credentials"),
            SecretBindingManifest(name="public_key", required=False),
            SecretBindingManifest(name="webhook_signing_secret", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("financial.payment_intent",),
            emits=("financial.payment_provider_observation",),
            classifications=("financial", "customer_contact"),
        ),
        egress=EgressManifest(hosts=("api.flutterwave.com",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="3cx",
        name="3CX",
        version="1.0.0",
        connector_type="voice",
        description="Embedded PBX integration frame.",
        runtime=RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
    ),
    ConnectorManifest(
        key="freepbx",
        name="FreePBX",
        version="1.0.0",
        connector_type="voice",
        description="Embedded PBX integration frame.",
        runtime=RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
    ),
)

_DEFINITION_BY_KEY = {definition.key: definition for definition in _DEFINITIONS}
if len(_DEFINITION_BY_KEY) != len(_DEFINITIONS):  # pragma: no cover - import guard
    raise RuntimeError("connector definition keys must be unique")


def connector_definitions() -> tuple[ConnectorManifest, ...]:
    """Return the deterministic, validated deployed connector catalogue."""

    return _DEFINITIONS


def connector_definition(key: str) -> ConnectorManifest | None:
    return _DEFINITION_BY_KEY.get(key.strip().lower())


def require_connector_definition(key: str) -> ConnectorManifest:
    definition = connector_definition(key)
    if definition is None:
        raise KeyError(f"unknown connector definition: {key}")
    return definition


def definitions_for_capability(capability_id: str) -> tuple[ConnectorManifest, ...]:
    return tuple(
        definition
        for definition in _DEFINITIONS
        if definition.capability(capability_id) is not None
    )


def _module_file_size(module_name: str | None) -> int:
    if not module_name:
        return 0
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return 0
    origin = spec.origin if spec is not None else None
    if not origin:
        return 0
    try:
        from pathlib import Path

        return int(Path(origin).stat().st_size)
    except OSError:
        return 0


def discover_connectors() -> list[ConnectorRegistryEntry]:
    """Project validated definitions into the legacy marketplace card shape.

    The function name remains for compatibility, but discovery is explicit and
    deterministic. Adding a file to the connectors directory no longer grants
    it catalogue presence or executable authority.
    """

    entries = [
        ConnectorRegistryEntry(
            key=definition.key,
            name=definition.name,
            version=definition.version,
            connector_type=definition.connector_type,
            description=definition.description,
            module_name=definition.runtime.module or f"catalog:{definition.key}",
            file_size_bytes=_module_file_size(definition.runtime.module),
        )
        for definition in _DEFINITIONS
        if definition.catalogue_visible
    ]
    return sorted(entries, key=lambda item: item.name.lower())
