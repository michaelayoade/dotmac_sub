"""Versioned connector-definition manifest contract.

Definitions are deployed application/artifact facts. Database installation
rows may reference their digest, but cannot grant a connector capabilities its
validated manifest does not declare.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLATFORM_API_VERSION = "dotmac.io/integrations/v1"
_CONNECTOR_KEY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ConnectorRuntimeType(StrEnum):
    """Deployment/runtime trust tier for a connector definition."""

    builtin_worker = "builtin_worker"
    external_oci = "external_oci"
    legacy_adapter = "legacy_adapter"
    catalogue_only = "catalogue_only"


class CapabilityMode(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    event = "event"
    inbound = "inbound"
    interactive = "interactive"
    reconcile = "reconcile"


class CapabilityManifest(BaseModel):
    """One typed domain port implemented by a connector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    modes: tuple[CapabilityMode, ...]
    description: str = ""

    @model_validator(mode="after")
    def validate_contract(self) -> CapabilityManifest:
        if not _CAPABILITY_ID_RE.fullmatch(self.id):
            raise ValueError(
                "capability id must be a dotted name ending in a positive vN"
            )
        if not self.modes:
            raise ValueError("capability must declare at least one execution mode")
        if len(set(self.modes)) != len(self.modes):
            raise ValueError("capability modes must be unique")
        return self


class SecretBindingManifest(BaseModel):
    """Named secret reference required by an installation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required: bool = True
    description: str = ""

    @model_validator(mode="after")
    def validate_name(self) -> SecretBindingManifest:
        if not _SECRET_NAME_RE.fullmatch(self.name):
            raise ValueError("secret binding name must be lower snake_case")
        return self


class RuntimeManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ConnectorRuntimeType
    module: str | None = None
    image: str | None = None
    digest: str | None = None

    @model_validator(mode="after")
    def validate_runtime_location(self) -> RuntimeManifest:
        if (
            self.type
            in {
                ConnectorRuntimeType.builtin_worker,
                ConnectorRuntimeType.legacy_adapter,
            }
            and not self.module
        ):
            raise ValueError(f"{self.type.value} runtime requires a module")
        if self.type == ConnectorRuntimeType.external_oci:
            if not self.image or not self.digest:
                raise ValueError("external_oci runtime requires image and digest")
            if not self.digest.startswith("sha256:"):
                raise ValueError("external_oci digest must be sha256-pinned")
        if self.type == ConnectorRuntimeType.catalogue_only and (
            self.module or self.image or self.digest
        ):
            raise ValueError("catalogue_only runtime cannot name executable code")
        return self


class DataAccessManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reads: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    classifications: tuple[str, ...] = ()


class EgressManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = ()
    allow_installation_hosts: bool = False

    @model_validator(mode="after")
    def validate_hosts(self) -> EgressManifest:
        for host in self.hosts:
            if "://" in host or "/" in host or not host.strip():
                raise ValueError("egress hosts must be bare non-empty hostnames")
        if len(set(self.hosts)) != len(self.hosts):
            raise ValueError("egress hosts must be unique")
        return self


class HealthManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str | None = None


class ConnectorManifest(BaseModel):
    """Immutable, validated connector definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["dotmac.io/integrations/v1"] = "dotmac.io/integrations/v1"
    key: str
    name: str
    version: str
    connector_type: str
    description: str
    catalogue_visible: bool = True
    runtime: RuntimeManifest
    capabilities: tuple[CapabilityManifest, ...] = ()
    config_schema: dict[str, Any] = Field(default_factory=dict)
    secrets: tuple[SecretBindingManifest, ...] = ()
    data_access: DataAccessManifest = Field(default_factory=DataAccessManifest)
    egress: EgressManifest = Field(default_factory=EgressManifest)
    health: HealthManifest = Field(default_factory=HealthManifest)

    @model_validator(mode="after")
    def validate_definition(self) -> ConnectorManifest:
        if not _CONNECTOR_KEY_RE.fullmatch(self.key):
            raise ValueError("connector key must be a stable lower-case identifier")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("connector version must be semantic versioning")
        if not self.name.strip() or not self.description.strip():
            raise ValueError("connector name and description are required")
        capability_ids = [capability.id for capability in self.capabilities]
        if len(set(capability_ids)) != len(capability_ids):
            raise ValueError("connector capability ids must be unique")
        secret_names = [binding.name for binding in self.secrets]
        if len(set(secret_names)) != len(secret_names):
            raise ValueError("connector secret binding names must be unique")
        if (
            self.runtime.type == ConnectorRuntimeType.catalogue_only
            and self.capabilities
        ):
            raise ValueError("catalogue-only connectors cannot declare capabilities")
        return self

    @property
    def digest(self) -> str:
        return connector_manifest_digest(self)

    def capability(self, capability_id: str) -> CapabilityManifest | None:
        return next(
            (
                capability
                for capability in self.capabilities
                if capability.id == capability_id
            ),
            None,
        )

    @property
    def required_secret_names(self) -> frozenset[str]:
        return frozenset(binding.name for binding in self.secrets if binding.required)


def connector_manifest_digest(manifest: ConnectorManifest) -> str:
    """Return the stable SHA-256 digest of one validated manifest."""

    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
