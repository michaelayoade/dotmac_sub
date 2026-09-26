"""Read-only admin projection for the Automation Center control plane."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services import (
    automation_actions,
    automation_capabilities,
    automation_rules,
    automation_runtime,
)
from app.services.automation_contracts import AutomationModuleManifest
from app.services.operator_tenant import OPERATOR_TENANT_ID


@dataclass(frozen=True, slots=True)
class AutomationModuleRow:
    manifest: AutomationModuleManifest
    state: str
    state_label: str


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
) -> dict[str, object]:
    """Build one permission-aware, tenant-scoped hub projection."""

    manifests = automation_capabilities.all_module_manifests()
    modules = tuple(_module_row(manifest) for manifest in manifests)
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
    authorized = "*" in permission_keys
    rule_builder_available = (
        can_create_rules
        and any(
            (authorized or trigger.author_permission in permission_keys)
            and any(
                action.entity_type == trigger.entity_type
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
        "module_count": len(modules),
        "declared_module_count": declared_count,
        "ready_module_count": ready_count,
        "runtime_state": runtime_state,
        "registry_errors": registry_errors,
        "rules": rules,
        "runs": runs,
        "legacy_surfaces": legacy_surfaces,
        "can_read_rules": can_read_rules,
        "can_read_runs": can_read_runs,
        "can_create_rules": can_create_rules,
        "can_update_rules": can_update_rules,
        "can_publish_rules": can_publish_rules,
        "can_operate_rules": can_operate_rules,
        "authoring_available": ready_count > 0 and not registry_errors,
        "rule_builder_available": rule_builder_available,
    }


__all__ = ["AutomationModuleRow", "build_automation_center_data"]
