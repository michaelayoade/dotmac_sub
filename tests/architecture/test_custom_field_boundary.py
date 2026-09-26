from __future__ import annotations

from pathlib import Path

from app.services import custom_field_capabilities
from app.services.custom_field_permissions import permission_granted
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_custom_field_owner_is_fully_contracted() -> None:
    owner = service_relationship("custom_fields.records")
    assert owner.module == "app.services.custom_fields"
    assert owner.is_contracted
    assert owner.depends_on == (
        "custom_fields.capability_registry",
        "events.dispatcher",
        "observability.audit_log",
    )


def test_registry_is_code_owned_and_registered_targets_are_explicit() -> None:
    assert custom_field_capabilities.capability_registry_errors() == ()
    targets = {
        target.key
        for manifest in custom_field_capabilities.registered_module_manifests()
        for target in manifest.targets
    }
    assert targets == {
        "subscriber",
        "project",
        "support_ticket",
        "work_order",
        "lead",
        "quote",
        "sales_order",
    }
    assert (
        custom_field_capabilities.target_capability("subscriber").write_permission
        == "customer:update"
    )
    source = _source("app/services/custom_field_capabilities.py")
    assert "DOMAIN_SOT_RELATIONSHIPS" in source
    assert "database" not in source.casefold()


def test_models_and_migration_are_tenant_isolated_and_granular() -> None:
    migration = _source("alembic/versions/623_custom_fields_center.py")
    model = _source("app/models/custom_fields.py")
    assert "custom_field_definitions" in model
    assert "custom_field_values" in model
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "app_current_tenant_id()" in migration
    assert "guard_custom_field_definition_lifecycle" in migration
    for permission in (
        "custom_fields:hub:read",
        "custom_fields:definition:read",
        "custom_fields:definition:create",
        "custom_fields:definition:update",
        "custom_fields:definition:activate",
        "custom_fields:definition:retire",
        "custom_fields:value:read",
        "custom_fields:value:write",
        "custom_fields:sensitive:read",
        "custom_fields:sensitive:write",
    ):
        assert permission in migration


def test_public_writes_use_owner_boundary_and_emit_value_free_evidence() -> None:
    source = _source("app/services/custom_fields.py")
    assert source.count("execute_owner_command(") == 4
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "stage_audit_event(" in source
    assert "EventType.custom_field_definition_changed" in source
    assert "EventType.custom_field_value_changed" in source
    assert '"value":' not in source[source.index("metadata = {") :]


def test_admin_and_api_adapters_are_permission_gated() -> None:
    web = _source("app/web/admin/custom_fields.py")
    api = _source("app/api/custom_fields.py")
    routes = _source("app/web/admin/__init__.py")
    navigation = _source("templates/components/navigation/admin_sidebar.html")
    settings_hub = _source("app/services/web_system_settings_hub.py")
    main = _source("app/main.py")
    assert "router.include_router(custom_fields_router)" in routes
    assert '"url": "/admin/custom-fields"' in settings_hub
    assert '"permission": "custom_fields:hub:read"' in settings_hub
    assert 'nav_link("Custom Fields"' not in navigation
    assert '"custom-fields-center"' in navigation
    assert '("app.api.custom_fields", "router", "api", "user")' in main
    for permission in (
        "HUB_READ_PERMISSION",
        "DEFINITION_CREATE_PERMISSION",
        "DEFINITION_UPDATE_PERMISSION",
        "DEFINITION_ACTIVATE_PERMISSION",
        "DEFINITION_RETIRE_PERMISSION",
    ):
        assert f"custom_fields.{permission}" in web
    assert "VALUE_READ_PERMISSION" in api
    assert "VALUE_WRITE_PERMISSION" in api


def test_permission_matching_honors_registered_wildcards() -> None:
    assert permission_granted(
        frozenset({"custom_fields:*"}), "custom_fields:value:write"
    )
    assert permission_granted(frozenset({"legacy:*"}), "legacy:lead:read")
    assert not permission_granted(frozenset({"legacy:lead:read"}), "legacy:lead:write")


def test_legacy_subscriber_fields_are_not_migrated_or_dual_written() -> None:
    migration = _source("alembic/versions/623_custom_fields_center.py")
    service = _source("app/services/custom_fields.py")
    design = _source("docs/designs/CUSTOM_FIELDS_CENTER_SOT.md")
    assert "subscriber_custom_fields" not in migration
    assert "SubscriberCustomField" not in service
    assert "no backfill" in design
    assert "dual-write" in design
