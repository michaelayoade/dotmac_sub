"""Product-owned capability contract carried by product-port descriptor v3.

The Integrator validates and transports this declaration; it does not author
it.  JSON Schemas remain open mappings because the owning Sub domain defines
their members.  A dated grace is explicit, enumerable debt and never an empty
schema pretending to validate a payload.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class CapabilityContractDeprecationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replaced_by: str = Field(min_length=1, max_length=120)
    retire_after: date
    reason: str


class CapabilitySchemaGraceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    retire_after: date
    tracked_by: str


class CapabilityContractDocument(BaseModel):
    """Exact a16 payload declaration nested inside descriptor v3."""

    model_config = ConfigDict(extra="forbid")

    command_schema: dict[str, object] | None
    result_schema: dict[str, object] | None
    observation_schema: dict[str, object] | None
    deprecation: CapabilityContractDeprecationDocument | None
    schema_grace: CapabilitySchemaGraceDocument | None
    contract_digest: str | None = Field(pattern=r"^[0-9a-f]{64}$")
