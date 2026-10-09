from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_hub_route_is_permission_gated_and_uses_the_capability_rule_builder() -> None:
    source = _source("app/web/admin/automation_center.py")
    assert 'require_permission("automation:hub:read")' in source
    assert "@router.get" in source
    assert "@router.post" in source
    assert "RULE_READ_PERMISSION" in source
    assert "RULE_CREATE_PERMISSION" in source
    assert "RUN_READ_PERMISSION" in source
    assert '"/rules"' in source
    assert "action.author_permission" in source
    support_declaration = _source(
        "app/services/sot_registry/domains/support_operations.py"
    )
    assert 'author_permission="support:ticket:update"' in support_declaration


def test_hub_is_registered_and_visible_only_with_hub_permission() -> None:
    routes = _source("app/web/admin/__init__.py")
    navigation = _source("templates/components/navigation/admin_sidebar.html")
    assert "router.include_router(automation_center_router)" in routes
    assert 'permission="automation:hub:read"' in navigation
    assert 'href="/admin/automation"' not in navigation
    assert '"/admin/automation"' in navigation


def test_hub_is_a_directory_and_keeps_diagnostics_out_of_the_default_workspace() -> (
    None
):
    template = _source("templates/admin/automation/index.html")
    for heading in (
        "Workflows",
        "Client scripts",
        "Server scripts",
        "Execution history",
        "Automation runtime dormant",
    ):
        assert heading in template
    assert "Business automation catalogue" not in template
    assert "Module registry" not in template
    assert "Existing automation ownership" not in template
    assert 'href="/admin/automation/workflows"' in template
    assert 'href="/admin/automation/client-scripts/manage"' in template
    assert 'href="/admin/automation/server-scripts"' in template


def test_hub_shows_support_communications_readiness_and_next_step() -> None:
    template = _source("templates/admin/automation/index.html")
    projection = _source("app/services/web_automation_center.py")
    assert 'AutomationCatalogState.unavailable: "Unavailable"' in projection
    assert 'AutomationCatalogState.managed_elsewhere: "Managed elsewhere"' in projection
    assert 'AutomationCatalogState.retired: "Retired"' in projection
    assert (
        "registry diagnostics are intentionally kept out of the operator workspace"
        in template
    )


def test_runtime_health_card_uses_semantic_status_and_safe_responsive_layout() -> None:
    template = _source("templates/admin/automation/index.html")
    design_system = _source("static/css/design-system.css")
    compiled_css = _source("static/css/main.css")
    assert "status-panel-positive" in template
    assert "status-panel-negative" in template
    assert "status-panel-warning" in template
    assert "status-foreground" in template
    assert 'class="p-5"' in template
    assert ".dark .status-panel-positive" in design_system
    assert ".dark .status-panel-negative" in design_system
    assert ".dark .status-panel-warning" in design_system
    assert ".grid-cols-3" in compiled_css
    assert ".dark\\:bg-slate-800" in compiled_css


def test_rule_builder_uses_registered_options_and_supports_multiple_steps() -> None:
    template = _source("templates/admin/automation/rule_builder.html")
    assert "builder_options|tojson" in template
    assert "Add condition" in template
    assert "Add group" in template
    assert "trigger-keys-json" in template
    assert 'multiple size="6"' in template
    assert 'group: "and"' in template
    assert "Add action" in template
    assert "conditions-availability" in template
    assert "actions-availability" in template
    assert "conditionButton.disabled" in template
    assert "actionButton.disabled" in template
    assert "This event has no condition fields" in template
    assert "actions-json" in template
    assert 'name="customer_scope" value="company"' in template
    assert 'name="customer_scope" value="selected"' in template
    assert "customer-search" in template
    assert "Saving creates a draft only" in template


def test_hub_exposes_all_mechanisms_and_target_readiness() -> None:
    template = _source("templates/admin/automation/index.html")
    workflows = _source("templates/admin/automation/workflows.html")
    client_scripts = _source("templates/admin/automation/script_list.html")
    script_builder = _source("templates/admin/automation/script_builder.html")
    route = _source("app/web/admin/automation_center.py")
    client_runtime = _source("static/js/automation-client-runtime.js")
    assert "Workflows" in workflows
    assert "updated_from" in client_scripts
    assert "updated_to" in client_scripts
    assert "target_groups" in route
    assert "Create client script" not in template
    assert "Create server script" not in template
    assert '"/workflows"' in route
    assert '"/client-scripts/manage"' in route
    assert '"/server-scripts"' in route
    assert "server_script_runtime_state" in template
    assert "JavaScript source" in script_builder
    assert "stable lowercase dotted identifier" in script_builder
    assert "Both mechanisms use JavaScript" in script_builder
    assert "no free-text event entry" in script_builder
    assert "published_client_scripts" in route
    assert "content_sha256" in client_runtime
    assert "data-automation-target" in client_runtime
    assert "database/write client" in client_runtime
    assert 'sandbox", "allow-scripts"' in client_runtime
    assert "connect-src 'none'" in client_runtime
    assert "opaque-origin" in client_runtime
    assert '"/scripts/{script_id}"' in route
    assert '"/scripts/{script_id}/edit"' in route
    assert '"/scripts/{script_id}/versions"' in route
    assert '"/scripts/{script_id}/status"' in route
    assert "create_script_version" in _source("app/services/automation_scripts.py")
    assert '_DEFAULT_TRIGGER = ""' in route
    assert "ticket-sla-suspension" not in route


def test_server_scripts_dispatch_from_declared_target_events_without_native_rules() -> (
    None
):
    handler = _source("app/services/events/handlers/automation.py")
    assert "_registered_script_targets" in handler
    assert "target.server_events" in handler
    assert "processed_script_targets" in handler
    assert "target_type=target.entity_type" in handler
    assert "event_name=event_name" in handler


def test_custom_field_surface_is_not_introduced() -> None:
    combined = "\n".join(
        _source(path)
        for path in (
            "app/services/web_automation_center.py",
            "app/web/admin/automation_center.py",
            "templates/admin/automation/index.html",
        )
    ).casefold()
    assert "custom field" not in combined
