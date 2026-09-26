"""Immutable module declarations consumed by the Custom Fields Center."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CustomFieldTargetCapability:
    """One entity type a domain deliberately exposes for custom fields."""

    key: str
    label: str
    entity_id_type: str
    read_permission: str
    write_permission: str
    detail_path_template: str
    maximum_active_fields: int = 50


@dataclass(frozen=True, slots=True)
class LegacyCustomFieldSurface:
    """Existing field storage that remains independently owned."""

    key: str
    label: str
    owner_service: str
    management_path: str | None
    migration_state: str


@dataclass(frozen=True, slots=True)
class CustomFieldDomainCapabilities:
    targets: tuple[CustomFieldTargetCapability, ...] = ()
    legacy_surfaces: tuple[LegacyCustomFieldSurface, ...] = ()
    manifest_version: int = 1


@dataclass(frozen=True, slots=True)
class CustomFieldModuleManifest:
    module_key: str
    label: str
    owner_domain: str
    registered: bool
    targets: tuple[CustomFieldTargetCapability, ...]
    legacy_surfaces: tuple[LegacyCustomFieldSurface, ...]
    manifest_version: int | None


__all__ = [
    "CustomFieldDomainCapabilities",
    "CustomFieldModuleManifest",
    "CustomFieldTargetCapability",
    "LegacyCustomFieldSurface",
]
