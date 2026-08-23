"""Sub-owned wire contract for Integrator settlement observations.

The outer envelope is provider-neutral and reusable by the Integrator.  The
settlement body is Sub's typed financial admission contract: the transport may
report facts, but it cannot select a local provider, invoice, account, payment,
or lifecycle consequence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

PRODUCT_OBSERVATION_SCHEMA_VERSION = "dotmac.io/product-observation/v1"
PRODUCT_PORT_DESCRIPTOR_SCHEMA_VERSION = "dotmac.io/product-port-descriptor/v2"
SETTLEMENT_CAPABILITY = "payments.settlement.observation.v1"


class IntegratorDestinationScope(BaseModel):
    """Sub's opaque local stream name, carried but never interpreted upstream."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=60)
    ref: str = Field(min_length=1, max_length=160)


class IntegratorObservationSource(BaseModel):
    """Durable engine provenance, never a connector-payload field."""

    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    connector_key: str = Field(min_length=1, max_length=160)


class ObservedMoney(BaseModel):
    """Exact provider-observed decimal and currency; no float coercion."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=Decimal("0"), max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ObservedFee(BaseModel):
    """A fee is optional because some provider contracts do not report one."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=Decimal("0"), max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class SettlementTransportEvidence(BaseModel):
    """Bounded provenance needed to correlate the provider transaction."""

    model_config = ConfigDict(extra="forbid")

    provider_event_type: str = Field(min_length=1, max_length=160)
    identity_source: str = Field(min_length=1, max_length=160)
    provider_transaction_id: str = Field(min_length=1, max_length=160)
    provider_webhook_id: str | None = Field(default=None, max_length=160)
    authentication_scheme: str | None = Field(default=None, max_length=160)
    payload_integrity: str | None = Field(default=None, max_length=160)


class SettlementObservation(BaseModel):
    """The normalized payment fact accepted by Sub's financial coordinator."""

    model_config = ConfigDict(extra="forbid")

    capability_id: Literal[SETTLEMENT_CAPABILITY]
    observation_kind: Literal["capture", "capture_failed"]
    provider_status: str = Field(min_length=1, max_length=120)
    amount: ObservedMoney
    provider_fee: ObservedFee | None = None
    occurred_at: datetime
    arrival_mode: Literal["ingress", "poll"]
    confirmation_evidence: Literal["connector_verified"]
    merchant_reference: str | None = Field(default=None, max_length=120)
    transport_evidence: SettlementTransportEvidence

    @model_validator(mode="after")
    def require_consistent_money(self) -> SettlementObservation:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.provider_fee is not None:
            if self.provider_fee.currency != self.amount.currency:
                raise ValueError("provider fee currency must match the gross amount")
            if self.provider_fee.amount > self.amount.amount:
                raise ValueError("provider fee cannot exceed the gross amount")
        return self


class IntegratorSettlementEnvelope(BaseModel):
    """Generic ProductObservation v1 carrying Sub's typed settlement body."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[PRODUCT_OBSERVATION_SCHEMA_VERSION]
    capability_id: Literal[SETTLEMENT_CAPABILITY]
    contract_version: Literal[1]
    source: IntegratorObservationSource
    provider_event_id: str = Field(min_length=1, max_length=255)
    event_type: Literal[SETTLEMENT_CAPABILITY]
    scope: IntegratorDestinationScope
    observation: SettlementObservation


class IntegratorSettlementReceipt(BaseModel):
    """Backward-compatible ProductPort answer understood by the delivery engine."""

    observation_id: str
    outcome: str
    processing_status: str
    replayed: bool


class IntegratorSettlementMirrorDisagreement(BaseModel):
    field: str
    integrator: str | None
    sub: str | None


class IntegratorSettlementMirrorReport(BaseModel):
    """Read-only cutover evidence; mirror requests never write a row."""

    verdict: Literal["match", "missing", "blocked"]
    identity: str
    counterpart_identity: str | None = None
    blocking_reasons: tuple[str, ...] = ()
    disagreements: tuple[IntegratorSettlementMirrorDisagreement, ...] = ()
    agrees: bool


class SettlementProductPortDescriptorV2(BaseModel):
    """Sub's authenticated v2 declaration for the generic observation wire."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[PRODUCT_PORT_DESCRIPTOR_SCHEMA_VERSION]
    application: Literal["sub"]
    owner_module: str = Field(min_length=1, max_length=160)
    capability_id: Literal[SETTLEMENT_CAPABILITY]
    capability_summary: str = Field(min_length=1, max_length=500)
    contract_version: Literal[1]
    destination_binding_id: UUID
    delivery_path: str = Field(pattern=r"^/")
    mirror_path: str = Field(pattern=r"^/")
    destination_scope: IntegratorDestinationScope
    activation_state: Literal[
        "configured_disabled", "enabled", "quarantined", "retired"
    ]
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
