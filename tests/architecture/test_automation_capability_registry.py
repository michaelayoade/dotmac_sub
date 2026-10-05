from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import automation_capabilities
from app.services.automation_contracts import (
    AutomationCatalogState,
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.model import DomainSOT


def test_every_sot_domain_is_visible_in_module_catalogue() -> None:
    modules = automation_capabilities.all_module_manifests()
    assert modules
    assert len({item.module_key for item in modules}) == len(modules)
    assert "automation_control_plane" in {item.module_key for item in modules}


def test_unregistered_trigger_cannot_resolve() -> None:
    with pytest.raises(automation_capabilities.AutomationCapabilityError):
        automation_capabilities.trigger_capability("support.ticket.deleted")


def test_checked_in_registry_is_structurally_valid() -> None:
    assert automation_capabilities.capability_registry_errors() == ()


def test_script_control_plane_emits_its_declared_change_event() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/automation_scripts.py").read_text(encoding="utf-8")
    event_types = (root / "app/services/events/types.py").read_text(encoding="utf-8")
    assert "EventType.automation_script_changed" in source
    assert 'automation_script_changed = "automation.script_changed"' in event_types


def test_requested_business_targets_are_declared_for_rule_or_script_authoring() -> None:
    manifests = automation_capabilities.all_module_manifests()
    targets = {
        target.entity_type
        for manifest in manifests
        for target in manifest.script_targets
    }
    assert {
        "customer.account",
        "sales.lead",
        "sales.quote",
        "sales.sales_order",
        "operations.project",
        "operations.work_order",
        "operations.material_request",
        "operations.vendor",
        "support.ticket",
    } <= targets


def test_script_targets_declare_event_identity_for_independent_server_dispatch() -> (
    None
):
    targets = {
        target.entity_type: target
        for manifest in automation_capabilities.all_module_manifests()
        for target in manifest.script_targets
    }
    expected_identity = {
        "customer.account": "subscriber_id",
        "sales.lead": "lead_id",
        "sales.quote": "quote_id",
        "sales.sales_order": "sales_order_id",
        "support.ticket": "ticket_id",
        "operations.project": "project_id",
        "operations.work_order": "work_order_id",
        "operations.material_request": "material_request_id",
        "operations.vendor": "project_id",
    }
    assert {
        entity_type: targets[entity_type].entity_id_field
        for entity_type in expected_identity
    } == expected_identity
    assert all(
        target.tenant_id_field == "tenant_id" and target.server_events
        for target in targets.values()
        if target.entity_type in expected_identity
    )


def test_material_request_automation_uses_assignable_owner_permissions() -> None:
    manifests = automation_capabilities.all_module_manifests()
    targets = {
        target.entity_type: target
        for manifest in manifests
        for target in manifest.script_targets
    }
    triggers = {
        trigger.key: trigger for manifest in manifests for trigger in manifest.triggers
    }
    actions = {
        action.key: action for manifest in manifests for action in manifest.actions
    }
    seed_path = Path(__file__).resolve().parents[2] / "scripts/seed/seed_rbac.py"
    seed_tree = ast.parse(
        seed_path.read_text(encoding="utf-8"), filename=str(seed_path)
    )
    seed_assignment = next(
        node
        for node in seed_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_PERMISSIONS"
            for target in node.targets
        )
    )
    seeded_permissions = {
        key for key, _description in ast.literal_eval(seed_assignment.value)
    }

    target = targets["operations.material_request"]
    assert target.read_permission == "operations:material_request:read"
    assert target.write_permission == "operations:material_request:write"
    assert (
        triggers["operations.material_request.cancellation_requested"].author_permission
        == target.read_permission
    )
    assert (
        actions["operations.material_request.enqueue_cancellation"].author_permission
        == target.write_permission
    )
    assert {target.read_permission, target.write_permission} <= seeded_permissions


def test_rule_actions_report_typed_adapter_readiness_by_module() -> None:
    manifests = automation_capabilities.all_module_manifests()
    actions = {
        action.key: action for manifest in manifests for action in manifest.actions
    }
    for key in (
        "customer.account.set_status",
        "sales.lead.set_status",
        "sales.quote.set_status",
        "sales.sales_order.set_status",
        "operations.project.set_status",
        "operations.material_request.enqueue_cancellation",
    ):
        assert key in actions
        assert actions[key].runtime_enabled is True
    assert actions["operations.work_order.set_status"].runtime_enabled is True
    assert actions["operations.vendor.set_status"].runtime_enabled is True
    assert actions["sales.lead.set_status"].inputs[0].enum_values
    assert actions["sales.quote.set_status"].inputs[0].enum_values == (
        "draft",
        "sent",
        "rejected",
        "expired",
    )
    assert actions["sales.sales_order.set_status"].inputs[0].enum_values == (
        "draft",
        "confirmed",
        "cancelled",
    )


def test_work_order_script_update_event_has_a_native_producer() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/work_order_commands.py").read_text(encoding="utf-8")
    assert "def _emit_work_order_updated_event" in source
    assert '"name": "work_order.updated"' in source
    assert "_emit_work_order_updated_event(" in source


def test_support_and_communications_catalogue_shows_readiness_and_existing_owners() -> (
    None
):
    modules = automation_capabilities.all_module_manifests()
    items = {item.key: item for module in modules for item in module.catalog_items}

    assert (
        items["support.ticket.center_rules"].state is AutomationCatalogState.available
    )
    assert items["support.ticket.center_rules"].trigger_keys == (
        "support.ticket.created",
        "support.ticket.assigned",
        "support.ticket.status_changed",
        "support.ticket.priority_changed",
        "support.ticket.resolution_requested",
        "support.ticket.resolution_confirmed",
        "support.ticket.resolution_disputed",
    )
    assert items["support.ticket.assignment_rules"].state is (
        AutomationCatalogState.managed_elsewhere
    )
    assert items["communications.inbox_automation_rules"].state is (
        AutomationCatalogState.unavailable
    )
    assert items["communications.retired_stale_auto_resolution"].state is (
        AutomationCatalogState.retired
    )
    assert all(item.explanation.strip() for item in items.values())


def test_customer_and_support_workflows_expose_owner_produced_events() -> None:
    manifests = automation_capabilities.all_module_manifests()
    triggers = {
        trigger.key: trigger for manifest in manifests for trigger in manifest.triggers
    }

    assert {
        "customer.account.created",
        "customer.account.updated",
        "customer.account.status_changed",
        "customer.account.suspended",
        "customer.account.reactivated",
        "support.ticket.created",
        "support.ticket.assigned",
        "support.ticket.status_changed",
        "support.ticket.priority_changed",
        "support.ticket.resolution_requested",
        "support.ticket.resolution_confirmed",
        "support.ticket.resolution_disputed",
    } <= triggers.keys()
    assert any(
        field.key == "status"
        for field in triggers["customer.account.status_changed"].fields
    )
    assert any(
        field.key == "status"
        for field in triggers["support.ticket.status_changed"].fields
    )
    assert any(
        field.key == "service_team_id"
        for field in triggers["support.ticket.assigned"].fields
    )

    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    support_source = (root / "app/services/support.py").read_text(encoding="utf-8")
    assert '"ticket.status_changed"' in support_source
    assert '"ticket.priority_changed"' in support_source
    assert '"customer_id"' in support_source


def test_available_catalogue_item_must_name_declared_runtime_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.automation_contracts import AutomationCatalogItem

    declaration = DomainSOT(
        domain="invalid_test_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            catalog_items=(
                AutomationCatalogItem(
                    key="test.available",
                    label="Test available",
                    group="Test",
                    state=AutomationCatalogState.available,
                    explanation="Must name a known runtime trigger and action.",
                    trigger_keys=("missing.trigger",),
                    action_keys=("missing.action",),
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities,
        "DOMAIN_SOT_RELATIONSHIPS",
        (declaration,),
    )
    assert automation_capabilities.capability_registry_errors() == (
        "automation catalogue item 'test.available' names an undeclared action",
        "automation catalogue item 'test.available' names an undeclared trigger",
    )


def test_invalid_enum_field_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = DomainSOT(
        domain="invalid_test_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.entity",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.entity.created",
                    label="Entity created",
                    event_type="test.entity.created",
                    event_schema_version=1,
                    entity_type="test.entity",
                    tenant_id_field="tenant_id",
                    entity_id_field="entity_id",
                    fields=(
                        AutomationConditionField(
                            key="status",
                            label="Status",
                            value_type=AutomationValueType.enum,
                            operators=(AutomationOperator.equals,),
                        ),
                    ),
                    author_permission="test:entity:read",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities,
        "DOMAIN_SOT_RELATIONSHIPS",
        (declaration,),
    )
    assert automation_capabilities.capability_registry_errors() == (
        "trigger 'test.entity.created' enum field 'status' has no values",
    )
