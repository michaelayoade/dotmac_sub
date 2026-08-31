"""Sub-owned declaration of the Integrator product-port destination.

The Integrator transports this declaration; it does not author it. The binding
UUID is a live Sub identity, while capability meaning and destination scope are
Sub-owned facts. Publishing them together prevents an assembly from permanently
transcribing two independent sources into configuration.

This service is read-only. A named reconciler stores the immutable snapshot and
compares ``descriptor_digest`` on refresh; drift repair never depends on an
event that might not arrive.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.services.domain_errors import DomainError
from app.services.integrations.connectors.integrator_http import (
    INTEGRATOR_CONNECTOR_KEY,
    INTEGRATOR_RECEIVE_CAPABILITY,
    INTEGRATOR_SETTLEMENT_CAPABILITY,
)

DESCRIPTOR_SCHEMA_VERSION = "dotmac.io/product-port-descriptor/v1"
SETTLEMENT_DESCRIPTOR_SCHEMA_VERSION = "dotmac.io/product-port-descriptor/v2"
CONTRACT_DESCRIPTOR_SCHEMA_VERSION = "dotmac.io/product-port-descriptor/v3"
MESSAGING_WIRE_SCHEMA_VERSION = "dotmac.io/integrator-observation-envelope/v1"
SETTLEMENT_WIRE_SCHEMA_VERSION = "dotmac.io/product-observation/v1"
APPLICATION = "sub"
OWNER_MODULE = "communications.team_inbox_integrator_envelope"
CAPABILITY_SUMMARY = "Inbound provider message and delivery-state observations"
CONTRACT_VERSION = 1
SETTLEMENT_OWNER_MODULE = "financial.payment_webhooks"
SETTLEMENT_CAPABILITY_SUMMARY = (
    "Verified provider settlement observations for the financial owner"
)
SCHEMA_GRACE_RETIRE_AFTER = date(2026, 9, 30)


class ProductPortDescriptorError(DomainError):
    """The requested row is not Sub's declared Integrator product port."""


class ProductPortActivationState(StrEnum):
    CONFIGURED_DISABLED = "configured_disabled"
    ENABLED = "enabled"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class DestinationScope:
    """Sub's opaque local name for this stream; transports never interpret it."""

    kind: str
    ref: str


@dataclass(frozen=True, slots=True)
class CapabilityContractDeprecationDocument:
    replaced_by: str
    retire_after: date
    reason: str


@dataclass(frozen=True, slots=True)
class CapabilitySchemaGraceDocument:
    reason: str
    retire_after: date
    tracked_by: str


@dataclass(frozen=True, slots=True)
class CapabilityContractDocument:
    """The exact a16 contract shape owned and published by a Sub domain."""

    command_schema: dict[str, object] | None
    result_schema: dict[str, object] | None
    observation_schema: dict[str, object] | None
    deprecation: CapabilityContractDeprecationDocument | None
    schema_grace: CapabilitySchemaGraceDocument | None
    contract_digest: str | None


@dataclass(frozen=True, slots=True)
class ProductPortDescriptorV1:
    schema_version: str
    application: str
    owner_module: str
    capability_id: str
    capability_summary: str
    contract_version: int
    destination_binding_id: UUID
    delivery_path: str
    mirror_path: str
    destination_scope: DestinationScope
    activation_state: str
    source_revision: str
    descriptor_digest: str


@dataclass(frozen=True, slots=True)
class ProductPortDescriptorV2:
    """Generic ProductObservation destination declared by Sub's finance owner."""

    schema_version: str
    application: str
    owner_module: str
    capability_id: str
    capability_summary: str
    contract_version: int
    destination_binding_id: UUID
    delivery_path: str
    mirror_path: str
    destination_scope: DestinationScope
    activation_state: str
    source_revision: str
    descriptor_digest: str


@dataclass(frozen=True, slots=True)
class ProductPortDescriptorV3:
    """Contract descriptor shared by both product wires, not their payloads."""

    schema_version: str
    wire_schema_version: str
    application: str
    owner_module: str
    capability_id: str
    capability_summary: str
    contract_version: int
    destination_binding_id: UUID
    delivery_path: str
    mirror_path: str
    destination_scope: DestinationScope
    activation_state: str
    source_revision: str
    capability_contract: CapabilityContractDocument
    descriptor_digest: str


DESTINATION_SCOPE = DestinationScope(kind="inbox", ref="support")
SETTLEMENT_DESTINATION_SCOPE = DestinationScope(
    kind="payment_provider_events", ref="verified"
)
MESSAGING_SCHEMA_GRACE = CapabilitySchemaGraceDocument(
    reason=(
        "The shared messaging.receive.v1 id is still served by divergent "
        "connector-normalized observation payloads; its successor contracts "
        "and exact connector digest claims are not published yet."
    ),
    retire_after=SCHEMA_GRACE_RETIRE_AFTER,
    tracked_by="docs/INTEGRATOR_MESSAGING_RECEIVE_CUTOVER.md#capability-schema-grace",
)
SETTLEMENT_SCHEMA_GRACE = CapabilitySchemaGraceDocument(
    reason=(
        "Paystack and Flutterwave do not yet publish one product-owned "
        "pre-delivery settlement observation schema with exact connector "
        "digest claims."
    ),
    retire_after=SCHEMA_GRACE_RETIRE_AFTER,
    tracked_by="docs/designs/INTEGRATION_PLATFORM_SOT.md#capability-schema-grace",
)
MESSAGING_CAPABILITY_CONTRACT = CapabilityContractDocument(
    command_schema=None,
    result_schema=None,
    observation_schema=None,
    deprecation=None,
    schema_grace=MESSAGING_SCHEMA_GRACE,
    contract_digest=None,
)
SETTLEMENT_CAPABILITY_CONTRACT = CapabilityContractDocument(
    command_schema=None,
    result_schema=None,
    observation_schema=None,
    deprecation=None,
    schema_grace=SETTLEMENT_SCHEMA_GRACE,
    contract_digest=None,
)


def descriptor_digest(document: Mapping[str, object]) -> str:
    """Canonical SHA-256 over every published descriptor field."""

    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capability_contract_document(
    contract: CapabilityContractDocument,
) -> dict[str, object]:
    """Serialize the domain declaration exactly as descriptor v3 carries it."""

    deprecation = contract.deprecation
    grace = contract.schema_grace
    return {
        "command_schema": contract.command_schema,
        "result_schema": contract.result_schema,
        "observation_schema": contract.observation_schema,
        "deprecation": (
            None
            if deprecation is None
            else {
                "replaced_by": deprecation.replaced_by,
                "retire_after": deprecation.retire_after.isoformat(),
                "reason": deprecation.reason,
            }
        ),
        "schema_grace": (
            None
            if grace is None
            else {
                "reason": grace.reason,
                "retire_after": grace.retire_after.isoformat(),
                "tracked_by": grace.tracked_by,
            }
        ),
        "contract_digest": contract.contract_digest,
    }


def _contract_descriptor(
    descriptor: ProductPortDescriptorV1 | ProductPortDescriptorV2,
    *,
    wire_schema_version: str,
    capability_contract: CapabilityContractDocument,
) -> ProductPortDescriptorV3:
    """Add the domain contract without changing the product delivery wire."""

    contract_document = capability_contract_document(capability_contract)
    source_revision = descriptor_digest(
        {
            "routing_source_revision": descriptor.source_revision,
            "wire_schema_version": wire_schema_version,
            "capability_contract": contract_document,
        }
    )
    published: dict[str, object] = {
        "schema_version": CONTRACT_DESCRIPTOR_SCHEMA_VERSION,
        "wire_schema_version": wire_schema_version,
        "application": descriptor.application,
        "owner_module": descriptor.owner_module,
        "capability_id": descriptor.capability_id,
        "capability_summary": descriptor.capability_summary,
        "contract_version": descriptor.contract_version,
        "destination_binding_id": descriptor.destination_binding_id,
        "delivery_path": descriptor.delivery_path,
        "mirror_path": descriptor.mirror_path,
        "destination_scope": {
            "kind": descriptor.destination_scope.kind,
            "ref": descriptor.destination_scope.ref,
        },
        "activation_state": descriptor.activation_state,
        "source_revision": source_revision,
        "capability_contract": contract_document,
    }
    return ProductPortDescriptorV3(
        schema_version=CONTRACT_DESCRIPTOR_SCHEMA_VERSION,
        wire_schema_version=wire_schema_version,
        application=descriptor.application,
        owner_module=descriptor.owner_module,
        capability_id=descriptor.capability_id,
        capability_summary=descriptor.capability_summary,
        contract_version=descriptor.contract_version,
        destination_binding_id=descriptor.destination_binding_id,
        delivery_path=descriptor.delivery_path,
        mirror_path=descriptor.mirror_path,
        destination_scope=descriptor.destination_scope,
        activation_state=descriptor.activation_state,
        source_revision=source_revision,
        capability_contract=capability_contract,
        descriptor_digest=descriptor_digest(published),
    )


def _activation_state(
    binding: IntegrationCapabilityBinding,
) -> ProductPortActivationState:
    installation = binding.installation
    if installation.state == IntegrationInstallationState.retired.value:
        return ProductPortActivationState.RETIRED
    if installation.state == IntegrationInstallationState.quarantined.value:
        return ProductPortActivationState.QUARANTINED
    if (
        installation.state == IntegrationInstallationState.enabled.value
        and binding.state == IntegrationBindingState.enabled.value
    ):
        return ProductPortActivationState.ENABLED
    return ProductPortActivationState.CONFIGURED_DISABLED


def product_port_descriptor(
    db: Session, destination_binding_id: UUID
) -> ProductPortDescriptorV1:
    """Return Sub's descriptor for one exact destination binding.

    Configured-but-disabled remains readable because importing the descriptor is
    a prerequisite for enabling delivery. Quarantined and retired bindings stay
    describable so the reconciler can report the actual drift state.
    """

    binding = db.get(IntegrationCapabilityBinding, destination_binding_id)
    if binding is None:
        raise ProductPortDescriptorError(
            code="communications.product_port_descriptor.not_found",
            message="Product port binding not found.",
        )
    installation = binding.installation
    if (
        installation.connector_key != INTEGRATOR_CONNECTOR_KEY
        or binding.capability_id != INTEGRATOR_RECEIVE_CAPABILITY
    ):
        raise ProductPortDescriptorError(
            code="communications.product_port_descriptor.not_found",
            message="Product port binding not found.",
        )

    activation_state = _activation_state(binding)
    source_revision = descriptor_digest(
        {
            "binding_id": binding.id,
            "binding_state": binding.state,
            "binding_updated_at": binding.updated_at,
            "installation_id": installation.id,
            "installation_state": installation.state,
            "installation_updated_at": installation.updated_at,
            "capability_id": INTEGRATOR_RECEIVE_CAPABILITY,
            "capability_summary": CAPABILITY_SUMMARY,
            "contract_version": CONTRACT_VERSION,
            "destination_scope": {
                "kind": DESTINATION_SCOPE.kind,
                "ref": DESTINATION_SCOPE.ref,
            },
        }
    )
    delivery_path = f"/api/v1/integration/observations/{binding.id}"
    mirror_path = f"{delivery_path}/mirror"
    published: dict[str, object] = {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "application": APPLICATION,
        "owner_module": OWNER_MODULE,
        "capability_id": INTEGRATOR_RECEIVE_CAPABILITY,
        "capability_summary": CAPABILITY_SUMMARY,
        "contract_version": CONTRACT_VERSION,
        "destination_binding_id": binding.id,
        "delivery_path": delivery_path,
        "mirror_path": mirror_path,
        "destination_scope": {
            "kind": DESTINATION_SCOPE.kind,
            "ref": DESTINATION_SCOPE.ref,
        },
        "activation_state": activation_state.value,
        "source_revision": source_revision,
    }
    return ProductPortDescriptorV1(
        schema_version=DESCRIPTOR_SCHEMA_VERSION,
        application=APPLICATION,
        owner_module=OWNER_MODULE,
        capability_id=INTEGRATOR_RECEIVE_CAPABILITY,
        capability_summary=CAPABILITY_SUMMARY,
        contract_version=CONTRACT_VERSION,
        destination_binding_id=binding.id,
        delivery_path=delivery_path,
        mirror_path=mirror_path,
        destination_scope=DESTINATION_SCOPE,
        activation_state=activation_state.value,
        source_revision=source_revision,
        descriptor_digest=descriptor_digest(published),
    )


def settlement_product_port_descriptor(
    db: Session, destination_binding_id: UUID
) -> ProductPortDescriptorV2:
    """Return the v2 descriptor for Sub's settlement observation port."""

    binding = db.get(IntegrationCapabilityBinding, destination_binding_id)
    if binding is None:
        raise ProductPortDescriptorError(
            code="financial.integrator_settlement_port.not_found",
            message="Product port binding not found.",
        )
    installation = binding.installation
    if (
        installation.connector_key != INTEGRATOR_CONNECTOR_KEY
        or binding.capability_id != INTEGRATOR_SETTLEMENT_CAPABILITY
    ):
        raise ProductPortDescriptorError(
            code="financial.integrator_settlement_port.not_found",
            message="Product port binding not found.",
        )

    activation_state = _activation_state(binding)
    source_revision = descriptor_digest(
        {
            "binding_id": binding.id,
            "binding_state": binding.state,
            "binding_updated_at": binding.updated_at,
            "installation_id": installation.id,
            "installation_state": installation.state,
            "installation_updated_at": installation.updated_at,
            "capability_id": INTEGRATOR_SETTLEMENT_CAPABILITY,
            "capability_summary": SETTLEMENT_CAPABILITY_SUMMARY,
            "contract_version": CONTRACT_VERSION,
            "destination_scope": {
                "kind": SETTLEMENT_DESTINATION_SCOPE.kind,
                "ref": SETTLEMENT_DESTINATION_SCOPE.ref,
            },
        }
    )
    delivery_path = f"/api/v1/integration/observations/payment-settlements/{binding.id}"
    mirror_path = f"{delivery_path}/mirror"
    published: dict[str, object] = {
        "schema_version": SETTLEMENT_DESCRIPTOR_SCHEMA_VERSION,
        "application": APPLICATION,
        "owner_module": SETTLEMENT_OWNER_MODULE,
        "capability_id": INTEGRATOR_SETTLEMENT_CAPABILITY,
        "capability_summary": SETTLEMENT_CAPABILITY_SUMMARY,
        "contract_version": CONTRACT_VERSION,
        "destination_binding_id": binding.id,
        "delivery_path": delivery_path,
        "mirror_path": mirror_path,
        "destination_scope": {
            "kind": SETTLEMENT_DESTINATION_SCOPE.kind,
            "ref": SETTLEMENT_DESTINATION_SCOPE.ref,
        },
        "activation_state": activation_state.value,
        "source_revision": source_revision,
    }
    return ProductPortDescriptorV2(
        schema_version=SETTLEMENT_DESCRIPTOR_SCHEMA_VERSION,
        application=APPLICATION,
        owner_module=SETTLEMENT_OWNER_MODULE,
        capability_id=INTEGRATOR_SETTLEMENT_CAPABILITY,
        capability_summary=SETTLEMENT_CAPABILITY_SUMMARY,
        contract_version=CONTRACT_VERSION,
        destination_binding_id=binding.id,
        delivery_path=delivery_path,
        mirror_path=mirror_path,
        destination_scope=SETTLEMENT_DESTINATION_SCOPE,
        activation_state=activation_state.value,
        source_revision=source_revision,
        descriptor_digest=descriptor_digest(published),
    )


def product_port_descriptor_v3(
    db: Session, destination_binding_id: UUID
) -> ProductPortDescriptorV3:
    """Publish messaging's a16 contract without retiring legacy descriptor v1."""

    return _contract_descriptor(
        product_port_descriptor(db, destination_binding_id),
        wire_schema_version=MESSAGING_WIRE_SCHEMA_VERSION,
        capability_contract=MESSAGING_CAPABILITY_CONTRACT,
    )


def settlement_product_port_descriptor_v3(
    db: Session, destination_binding_id: UUID
) -> ProductPortDescriptorV3:
    """Publish settlement's a16 contract without retiring legacy descriptor v2."""

    return _contract_descriptor(
        settlement_product_port_descriptor(db, destination_binding_id),
        wire_schema_version=SETTLEMENT_WIRE_SCHEMA_VERSION,
        capability_contract=SETTLEMENT_CAPABILITY_CONTRACT,
    )
