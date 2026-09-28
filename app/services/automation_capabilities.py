"""Canonical Automation Center module and capability registry.

The catalogue is derived from the source-of-truth domain registry. It is not
an editable database list and module feature flags are not authority claims.
Only explicitly declared triggers and actions can be used by rules.
"""

from __future__ import annotations

from collections import Counter

from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationCatalogState,
    AutomationConditionField,
    AutomationModuleManifest,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS


class AutomationCapabilityError(ValueError):
    """Raised when authoring names an undeclared or ambiguous capability."""


def _label(domain: str) -> str:
    return domain.replace("_", " ").title()


def all_module_manifests() -> tuple[AutomationModuleManifest, ...]:
    """Return every SOT domain, including domains not automation-enabled."""

    manifests: list[AutomationModuleManifest] = []
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        declaration = domain.automation
        manifests.append(
            AutomationModuleManifest(
                module_key=domain.domain,
                label=_label(domain.domain),
                owner_domain=domain.domain,
                registered=declaration is not None,
                target_types=declaration.target_types if declaration else (),
                triggers=declaration.triggers if declaration else (),
                actions=declaration.actions if declaration else (),
                legacy_surfaces=(declaration.legacy_surfaces if declaration else ()),
                catalog_items=(declaration.catalog_items if declaration else ()),
                script_targets=(declaration.script_targets if declaration else ()),
                manifest_version=(
                    declaration.manifest_version if declaration else None
                ),
            )
        )
    return tuple(manifests)


def registered_module_manifests() -> tuple[AutomationModuleManifest, ...]:
    return tuple(item for item in all_module_manifests() if item.registered)


def module_manifest(module_key: str) -> AutomationModuleManifest:
    normalized = module_key.strip().casefold()
    matches = [
        item
        for item in all_module_manifests()
        if item.module_key.casefold() == normalized
    ]
    if len(matches) != 1:
        raise AutomationCapabilityError(
            f"Automation module {module_key!r} is not declared exactly once."
        )
    return matches[0]


def trigger_capability(key: str) -> AutomationTriggerCapability:
    matches = [
        trigger
        for module in registered_module_manifests()
        for trigger in module.triggers
        if trigger.key == key
    ]
    if len(matches) != 1:
        raise AutomationCapabilityError(
            f"Automation trigger {key!r} is not declared exactly once."
        )
    return matches[0]


def action_capability(key: str) -> AutomationActionCapability:
    matches = [
        action
        for module in registered_module_manifests()
        for action in module.actions
        if action.key == key
    ]
    if len(matches) != 1:
        raise AutomationCapabilityError(
            f"Automation action {key!r} is not declared exactly once."
        )
    return matches[0]


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(key for key, count in Counter(values).items() if count > 1))


def _field_errors(
    *, trigger: AutomationTriggerCapability, field: AutomationConditionField
) -> list[str]:
    errors: list[str] = []
    if not field.key.strip() or not field.label.strip():
        errors.append(f"trigger {trigger.key!r} has a blank field key or label")
    if not field.operators:
        errors.append(f"trigger {trigger.key!r} field {field.key!r} has no operators")
    if field.value_type is AutomationValueType.enum and not field.enum_values:
        errors.append(f"trigger {trigger.key!r} enum field {field.key!r} has no values")
    return errors


def capability_registry_errors() -> tuple[str, ...]:
    """Return all structural errors; startup and CI fail on any result."""

    errors: list[str] = []
    manifests = all_module_manifests()
    errors.extend(
        f"duplicate automation module key {key!r}"
        for key in _duplicates(tuple(item.module_key for item in manifests))
    )
    registered = registered_module_manifests()
    triggers = tuple(trigger for item in registered for trigger in item.triggers)
    actions = tuple(action for item in registered for action in item.actions)
    legacy = tuple(surface for item in registered for surface in item.legacy_surfaces)
    catalog_items = tuple(item for module in manifests for item in module.catalog_items)
    errors.extend(
        f"duplicate automation trigger key {key!r}"
        for key in _duplicates(tuple(item.key for item in triggers))
    )
    errors.extend(
        f"duplicate automation catalogue item key {key!r}"
        for key in _duplicates(tuple(item.key for item in catalog_items))
    )
    errors.extend(
        f"duplicate automation action key {key!r}"
        for key in _duplicates(tuple(item.key for item in actions))
    )
    errors.extend(
        f"duplicate legacy automation surface key {key!r}"
        for key in _duplicates(tuple(item.key for item in legacy))
    )
    for manifest in registered:
        if manifest.manifest_version != 1:
            errors.append(
                f"automation module {manifest.module_key!r} uses unsupported "
                f"manifest version {manifest.manifest_version!r}"
            )
        errors.extend(
            f"automation module {manifest.module_key!r} repeats target {key!r}"
            for key in _duplicates(manifest.target_types)
        )
        errors.extend(
            f"automation module {manifest.module_key!r} repeats script target {key!r}"
            for key in _duplicates(
                tuple(target.key for target in manifest.script_targets)
            )
        )
        for target in manifest.script_targets:
            if not all(
                value.strip()
                for value in (target.key, target.label, target.entity_type)
            ):
                errors.append(
                    f"automation module {manifest.module_key!r} has an incomplete script target"
                )
            if not target.client_events and not target.server_events:
                errors.append(
                    f"automation script target {target.key!r} has no executable events"
                )
            if not target.read_permission.strip():
                errors.append(
                    f"automation script target {target.key!r} has no read permission"
                )
            if not target.tenant_id_field.strip():
                errors.append(
                    f"automation script target {target.key!r} has no tenant identity field"
                )
            if not target.entity_id_field.strip():
                errors.append(
                    f"automation script target {target.key!r} has no entity identity field"
                )
        for trigger in manifest.triggers:
            if trigger.entity_type not in manifest.target_types:
                errors.append(
                    f"trigger {trigger.key!r} uses undeclared target "
                    f"{trigger.entity_type!r}"
                )
            if trigger.event_schema_version < 1:
                errors.append(f"trigger {trigger.key!r} has invalid schema version")
            if any(
                version < 1 or version >= trigger.event_schema_version
                for version in trigger.compatible_event_schema_versions
            ):
                errors.append(
                    f"trigger {trigger.key!r} has invalid compatible event schemas"
                )
            if not trigger.tenant_id_field.strip():
                errors.append(f"trigger {trigger.key!r} has no tenant identity field")
            if not trigger.entity_id_field.strip():
                errors.append(f"trigger {trigger.key!r} has no entity identity field")
            if not trigger.author_permission.strip():
                errors.append(f"trigger {trigger.key!r} has no author permission")
            errors.extend(
                f"trigger {trigger.key!r} repeats field {key!r}"
                for key in _duplicates(tuple(field.key for field in trigger.fields))
            )
            for field in trigger.fields:
                errors.extend(_field_errors(trigger=trigger, field=field))
        for action in manifest.actions:
            if action.entity_type not in manifest.target_types:
                errors.append(
                    f"action {action.key!r} uses undeclared target "
                    f"{action.entity_type!r}"
                )
            if action.input_schema_version < 1:
                errors.append(f"action {action.key!r} has invalid schema version")
            if not all(
                value.strip()
                for value in (
                    action.command_owner,
                    action.command_name,
                    action.author_permission,
                    action.runtime_scope,
                    action.idempotency,
                )
            ):
                errors.append(f"action {action.key!r} has an incomplete contract")
            errors.extend(
                f"action {action.key!r} repeats input {key!r}"
                for key in _duplicates(tuple(item.key for item in action.inputs))
            )
            for action_input in action.inputs:
                if not action_input.key.strip() or not action_input.label.strip():
                    errors.append(
                        f"action {action.key!r} has a blank input key or label"
                    )
                if (
                    action_input.value_type is AutomationValueType.enum
                    and not action_input.enum_values
                ):
                    errors.append(
                        f"action {action.key!r} enum input "
                        f"{action_input.key!r} has no values"
                    )
                if (
                    action_input.value_type is not AutomationValueType.enum
                    and action_input.enum_values
                ):
                    errors.append(
                        f"action {action.key!r} non-enum input "
                        f"{action_input.key!r} declares enum values"
                    )
        for item in manifest.catalog_items:
            if not all(
                (
                    item.key.strip(),
                    item.label.strip(),
                    item.group.strip(),
                    item.explanation.strip(),
                )
            ):
                errors.append(
                    f"automation catalogue item {item.key!r} has a blank required field"
                )
            if (
                item.state is AutomationCatalogState.managed_elsewhere
                and not item.management_path
            ):
                errors.append(
                    f"automation catalogue item {item.key!r} has no management path"
                )
            if item.state is AutomationCatalogState.available:
                trigger_keys = {trigger.key for trigger in manifest.triggers}
                action_keys = {action.key for action in manifest.actions}
                if not item.trigger_keys or not item.action_keys:
                    errors.append(
                        f"available automation catalogue item {item.key!r} must name triggers and actions"
                    )
                if set(item.trigger_keys) - trigger_keys:
                    errors.append(
                        f"automation catalogue item {item.key!r} names an undeclared trigger"
                    )
                if set(item.action_keys) - action_keys:
                    errors.append(
                        f"automation catalogue item {item.key!r} names an undeclared action"
                    )
                if any(
                    not trigger.runtime_enabled
                    for trigger in manifest.triggers
                    if trigger.key in item.trigger_keys
                ) or any(
                    not action.runtime_enabled
                    for action in manifest.actions
                    if action.key in item.action_keys
                ):
                    errors.append(
                        f"available automation catalogue item {item.key!r} uses an unavailable runtime"
                    )
    return tuple(sorted(errors))


def require_valid_capability_registry() -> None:
    errors = capability_registry_errors()
    if errors:
        raise AutomationCapabilityError("; ".join(errors))


__all__ = [
    "AutomationCapabilityError",
    "action_capability",
    "all_module_manifests",
    "capability_registry_errors",
    "module_manifest",
    "registered_module_manifests",
    "require_valid_capability_registry",
    "trigger_capability",
]
