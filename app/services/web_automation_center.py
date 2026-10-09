"""Read-only admin projection for the Automation Center control plane."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services import (
    automation_actions,
    automation_capabilities,
    automation_rules,
    automation_runtime,
    automation_script_runtime,
    automation_scripts,
)
from app.services.automation_contracts import (
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationMechanism,
    AutomationModuleManifest,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID


@dataclass(frozen=True, slots=True)
class AutomationModuleRow:
    manifest: AutomationModuleManifest
    state: str
    state_label: str


@dataclass(frozen=True, slots=True)
class AutomationCatalogRow:
    item: AutomationCatalogItem
    module_label: str
    state: str
    state_label: str
    explanation: str


def _module_row(manifest: AutomationModuleManifest) -> AutomationModuleRow:
    if not manifest.registered:
        return AutomationModuleRow(manifest, "available", "Not registered")
    if not manifest.triggers or not manifest.actions:
        return AutomationModuleRow(manifest, "declared", "Declared only")
    if not any(action.runtime_enabled for action in manifest.actions):
        return AutomationModuleRow(manifest, "draft", "Draft authoring")
    action_errors = automation_actions.runtime_registry_errors()
    if action_errors:
        return AutomationModuleRow(manifest, "blocked", "Adapter mismatch")
    return AutomationModuleRow(manifest, "ready", "Runtime ready")


def _catalog_row(
    item: AutomationCatalogItem, module_label: str
) -> AutomationCatalogRow:
    state = item.state
    if state is AutomationCatalogState.available:
        triggers = {
            trigger.key: trigger
            for module in automation_capabilities.registered_module_manifests()
            for trigger in module.triggers
        }
        actions = {
            action.key: action
            for module in automation_capabilities.registered_module_manifests()
            for action in module.actions
        }
        ready = all(
            key in triggers and triggers[key].runtime_enabled
            for key in item.trigger_keys
        ) and all(
            key in actions and actions[key].runtime_enabled for key in item.action_keys
        )
        if ready:
            try:
                for action_key in item.action_keys:
                    automation_actions.action_executor(action_key)
            except automation_actions.AutomationActionExecutorError:
                ready = False
        if not ready:
            return AutomationCatalogRow(
                item,
                module_label,
                "unavailable",
                "Unavailable",
                "Developer support is incomplete: the approved trigger and action executor must be registered before admins can use this item.",
            )
    labels = {
        AutomationCatalogState.available: "Available in builder",
        AutomationCatalogState.unavailable: "Unavailable",
        AutomationCatalogState.managed_elsewhere: "Managed elsewhere",
        AutomationCatalogState.retired: "Retired",
    }
    return AutomationCatalogRow(
        item, module_label, state.value, labels[state], item.explanation
    )


def build_automation_center_data(
    db: Session,
    *,
    can_read_rules: bool,
    can_read_runs: bool,
    can_create_rules: bool,
    can_update_rules: bool,
    can_publish_rules: bool,
    can_operate_rules: bool,
    permission_keys: frozenset[str],
    can_read_scripts: bool = False,
    can_create_scripts: bool = False,
    can_update_scripts: bool = False,
    can_publish_scripts: bool = False,
) -> dict[str, object]:
    """Build one permission-aware, tenant-scoped hub projection."""

    manifests = automation_capabilities.all_module_manifests()
    modules = tuple(_module_row(manifest) for manifest in manifests)
    catalog_items = tuple(
        _catalog_row(item, manifest.label)
        for manifest in manifests
        for item in manifest.catalog_items
    )
    rules = (
        automation_rules.list_rules(
            db,
            automation_rules.ListAutomationRulesQuery(
                tenant_id=OPERATOR_TENANT_ID,
                include_retired=False,
            ),
        )
        if can_read_rules
        else ()
    )
    runs = (
        automation_runtime.list_runs(
            db,
            automation_runtime.ListAutomationRunsQuery(
                tenant_id=OPERATOR_TENANT_ID,
                limit=50,
            ),
        )
        if can_read_runs
        else ()
    )
    registry_errors = (
        *automation_capabilities.capability_registry_errors(),
        *automation_actions.runtime_registry_errors(),
    )
    ready_count = sum(row.state == "ready" for row in modules)
    declared_count = sum(row.manifest.registered for row in modules)
    runtime_state = (
        "blocked" if registry_errors else "ready" if ready_count else "dormant"
    )
    legacy_surfaces = tuple(
        surface for manifest in manifests for surface in manifest.legacy_surfaces
    )
    scripts = (
        automation_scripts.list_scripts(db, tenant_id=OPERATOR_TENANT_ID)
        if can_read_scripts
        else ()
    )
    script_targets = tuple(
        target
        for manifest in manifests
        if manifest.registered
        for target in manifest.script_targets
    )
    rule_target_matrix = tuple(
        {
            "module_label": manifest.label,
            "target_type": target_type,
            "triggers": tuple(
                trigger.key
                for trigger in manifest.triggers
                if trigger.entity_type == target_type and trigger.runtime_enabled
            ),
            "actions": tuple(
                action.key
                for action_manifest in manifests
                for action in action_manifest.actions
                if automation_capabilities.action_applies_to_entity(action, target_type)
                and action.authoring_enabled
                and action.runtime_enabled
            ),
            "script_target": any(
                target.entity_type == target_type for target in manifest.script_targets
            ),
        }
        for manifest in manifests
        if manifest.registered
        for target_type in manifest.target_types
    )
    authorized = "*" in permission_keys
    rule_builder_available = (
        can_create_rules
        and any(
            (authorized or trigger.author_permission in permission_keys)
            and any(
                automation_capabilities.action_applies_to_entity(
                    action, trigger.entity_type
                )
                and action.authoring_enabled
                and action.runtime_enabled
                and (authorized or action.author_permission in permission_keys)
                for candidate in manifests
                for action in candidate.actions
            )
            for manifest in manifests
            for trigger in manifest.triggers
        )
        and not automation_capabilities.capability_registry_errors()
    )
    return {
        "modules": modules,
        "catalog_items": catalog_items,
        "module_count": len(modules),
        "declared_module_count": declared_count,
        "ready_module_count": ready_count,
        "runtime_state": runtime_state,
        "registry_errors": registry_errors,
        "rules": rules,
        "scripts": scripts,
        "can_read_scripts": can_read_scripts,
        "can_create_scripts": can_create_scripts,
        "can_update_scripts": can_update_scripts,
        "can_publish_scripts": can_publish_scripts,
        "runs": runs,
        "legacy_surfaces": legacy_surfaces,
        "script_targets": script_targets,
        "script_target_count": len(script_targets),
        "rule_target_matrix": rule_target_matrix,
        "mechanisms": tuple(item.value for item in AutomationMechanism),
        "client_script_available": bool(
            can_create_scripts
            and any(target.client_events for target in script_targets)
        ),
        "server_script_available": bool(
            can_create_scripts
            and any(target.server_events for target in script_targets)
            and automation_script_runtime.runtime_state()
            is automation_script_runtime.AutomationScriptRuntimeState.ready
        ),
        "server_script_runtime_state": automation_script_runtime.runtime_state().value,
        "can_read_rules": can_read_rules,
        "can_read_runs": can_read_runs,
        "can_create_rules": can_create_rules,
        "can_update_rules": can_update_rules,
        "can_publish_rules": can_publish_rules,
        "can_operate_rules": can_operate_rules,
        "authoring_available": ready_count > 0 and not registry_errors,
        "rule_builder_available": rule_builder_available,
    }


__all__ = [
    "AutomationCatalogRow",
    "AutomationModuleRow",
    "build_automation_center_data",
]
