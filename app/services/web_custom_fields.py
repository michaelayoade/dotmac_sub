"""Permission-aware read projection for the Custom Fields Center."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import (
    custom_field_access,
    custom_field_capabilities,
    custom_field_targets,
    custom_fields,
)
from app.services.custom_field_contracts import CustomFieldModuleManifest
from app.services.custom_field_permissions import permission_granted
from app.services.operator_tenant import OPERATOR_TENANT_ID


@dataclass(frozen=True, slots=True)
class CustomFieldModuleRow:
    manifest: CustomFieldModuleManifest
    state: str
    state_label: str


def _module_row(manifest: CustomFieldModuleManifest) -> CustomFieldModuleRow:
    if not manifest.registered:
        return CustomFieldModuleRow(manifest, "available", "Not registered")
    if not manifest.targets:
        return CustomFieldModuleRow(manifest, "legacy", "Legacy only")
    if custom_field_targets.runtime_registry_errors():
        return CustomFieldModuleRow(manifest, "blocked", "Adapter mismatch")
    return CustomFieldModuleRow(manifest, "ready", "Ready")


def build_custom_fields_center_data(
    db: Session,
    *,
    can_read_definitions: bool,
    can_create_definitions: bool,
    can_update_definitions: bool,
    can_activate_definitions: bool,
    can_retire_definitions: bool,
    selected_target: str | None = None,
) -> dict[str, object]:
    manifests = custom_field_capabilities.all_module_manifests()
    modules = tuple(_module_row(manifest) for manifest in manifests)
    targets = tuple(
        target
        for manifest in manifests
        if manifest.registered
        for target in manifest.targets
    )
    selected = (
        selected_target
        if any(item.key == selected_target for item in targets)
        else None
    )
    definitions = (
        custom_fields.list_definitions(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            target_type=selected,
            include_retired=True,
        )
        if can_read_definitions
        else ()
    )
    registry_errors = (
        *custom_field_capabilities.capability_registry_errors(),
        *custom_field_targets.runtime_registry_errors(),
    )
    legacy_surfaces = tuple(
        surface for manifest in manifests for surface in manifest.legacy_surfaces
    )
    return {
        "modules": modules,
        "module_count": len(modules),
        "registered_module_count": sum(item.manifest.registered for item in modules),
        "ready_target_count": len(targets) if not registry_errors else 0,
        "targets": targets,
        "selected_target": selected,
        "definitions": definitions,
        "definition_count": len(definitions),
        "active_definition_count": sum(item.status == "active" for item in definitions),
        "draft_definition_count": sum(item.status == "draft" for item in definitions),
        "registry_errors": registry_errors,
        "legacy_surfaces": legacy_surfaces,
        "can_read_definitions": can_read_definitions,
        "can_create_definitions": can_create_definitions,
        "can_update_definitions": can_update_definitions,
        "can_activate_definitions": can_activate_definitions,
        "can_retire_definitions": can_retire_definitions,
    }


def build_target_value_context(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    permission_keys: frozenset[str],
    auth: dict | None = None,
) -> dict[str, object]:
    """Build the shared detail-page projection for one registered record."""

    target = custom_field_capabilities.target_capability(target_type)
    can_read = permission_granted(
        permission_keys, custom_fields.VALUE_READ_PERMISSION
    ) and permission_granted(permission_keys, target.read_permission)
    if can_read and auth is not None:
        can_read = custom_field_access.target_access_allowed(
            db, auth=auth, target_type=target.key, target_id=target_id, write=False
        )
    values = (
        tuple(
            row
            for row in custom_fields.list_target_values(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                target_type=target.key,
                target_id=target_id,
                permission_keys=permission_keys,
            )
            if row.definition.show_in_detail or row.definition.show_in_form
        )
        if can_read
        else ()
    )
    return {
        "custom_field_values": values,
        "custom_field_target_type": target.key,
        "custom_field_target_id": target_id,
        "custom_field_target_label": target.label,
        "can_write_custom_fields": (
            permission_granted(permission_keys, custom_fields.VALUE_WRITE_PERMISSION)
            and permission_granted(permission_keys, target.write_permission)
            and (
                auth is None
                or custom_field_access.target_access_allowed(
                    db,
                    auth=auth,
                    target_type=target.key,
                    target_id=target_id,
                    write=True,
                )
            )
        ),
        "can_write_sensitive_custom_fields": permission_granted(
            permission_keys, custom_fields.SENSITIVE_WRITE_PERMISSION
        ),
    }


__all__ = [
    "CustomFieldModuleRow",
    "build_custom_fields_center_data",
    "build_target_value_context",
]
