from __future__ import annotations

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
