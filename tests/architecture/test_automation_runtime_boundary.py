from __future__ import annotations

from pathlib import Path

from app.services import automation_actions
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_execution_coordinator_is_fully_contracted() -> None:
    service = service_relationship("automation.execution")
    assert service.module == "app.services.automation_runtime"
    assert service.is_contracted
    assert service.depends_on == (
        "automation.capability_registry",
        "automation.rule_definitions",
        "events.store",
        "events.replay_evidence",
    )


def test_replay_evidence_query_is_typed_and_read_only() -> None:
    service = service_relationship("events.replay_evidence")
    assert service.module == "app.services.event_replay_evidence"
    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"


def test_runtime_adapter_registry_is_closed_and_valid() -> None:
    source = _source("app/services/automation_actions.py")
    assert "MappingProxyType" in source
    assert automation_actions.runtime_registry_errors() == ()


def test_runtime_handler_is_registered_with_explicit_event_scope() -> None:
    dispatcher = _source("app/services/events/dispatcher.py")
    controls = _source("app/services/control_relationships.py")
    assert "dispatcher.register_handler(AutomationEventHandler())" in dispatcher
    assert '"AutomationEventHandler": HandlerControl(' in controls
    assert 'handler_name == "AutomationEventHandler"' in controls


def test_runtime_preserves_typed_action_retry_classification() -> None:
    source = _source("app/services/events/handlers/automation.py")
    assert "class AutomationEventHandlerError(DomainError)" in source
    assert "_handler_error(" in source
    assert "retryable=False" in source


def test_ticket_sla_consequence_delegates_only_to_pause_owner() -> None:
    source = _source("app/services/ticket_sla_service_automation.py")
    assert "account_lifecycle.pause_subscription_for_cause(" in source
    assert "account_lifecycle.suspend_subscription(" not in source
    assert "subscription.status = " not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_ticket_sla_enforcement_reason_is_migrated() -> None:
    migration = _source("alembic/versions/630_ticket_sla_enforcement_reason.py")
    assert "ADD VALUE IF NOT EXISTS 'ticket_sla'" in migration


def test_runtime_ledger_is_tenant_isolated_and_permissions_are_granular() -> None:
    migration = _source("alembic/versions/614_automation_runtime_ledger.py")
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "app_current_tenant_id()" in migration
    for permission in (
        "automation:hub:read",
        "automation:run:read",
        "automation:run:redrive",
    ):
        assert permission in migration


def test_manual_run_retry_is_audited_and_permission_gated() -> None:
    migration = _source("alembic/versions/625_automation_run_retry_audit.py")
    service = _source("app/services/automation_runtime.py")
    web = _source("app/web/admin/automation_center.py")
    assert "automation_run_retries" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "actor=command.context.actor" in service
    assert "AutomationRunRetry" in service
    assert '"/runs/{run_id}/retry"' in web
    assert "RUN_REDRIVE_PERMISSION" in web
    assert "execute_prepared_run" in service


def test_run_history_exposes_only_tenant_scoped_owner_projections() -> None:
    web = _source("app/web/admin/automation_center.py")
    detail = _source("templates/admin/automation/run_detail.html")
    history = _source("templates/admin/automation/run_history.html")
    assert '"/runs",' in web
    assert '"/runs/{run_id}"' in web
    assert "GetAutomationRunDetailQuery" in web
    assert "Affected record" in detail
    assert "Retry history" in detail
    assert "page_meta" in history
    assert "previous_url" in history
    assert "RUN_HISTORY_LIST" in _source("app/services/automation_runtime.py")


def test_runtime_does_not_mutate_legacy_rule_models() -> None:
    source = _source("app/services/automation_runtime.py")
    for legacy_model in (
        "TicketAssignmentRule",
        "AlertRule",
        "FupRule",
        "InboxAutomationRule",
        "NasConnectionRule",
        "DispatchRule",
    ):
        assert legacy_model not in source


def test_server_script_runtime_is_external_and_fail_closed() -> None:
    runtime = _source("app/services/automation_script_runtime.py")
    runner = _source("app/services/automation_script_runner.py")
    migration = _source("alembic/versions/626_automation_script_control_plane.py")
    assert "ExternalOciRunner" in runner
    assert "PodmanTransport" in runner
    assert "sha256:" in runtime
    assert "AutomationScriptRuntimeState.ready" in runner
    assert "exec(" not in runtime
    assert "eval(" not in runtime
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "app_current_tenant_id()" in migration
    for permission in (
        "automation:script:read",
        "automation:script:create",
        "automation:script:update",
        "automation:script:publish",
    ):
        assert permission in migration


def test_server_script_side_effects_reenter_typed_owner_action_boundary() -> None:
    runner = _source("app/services/automation_script_runner.py")
    handler = _source("app/services/events/handlers/automation.py")
    assert "parse_script_action_requests" in runner
    assert "AutomationScriptActionRequest" in runner
    assert "action_capability(request.action_key)" in handler
    assert "_script_action_inputs(action, request)" in handler
    assert "ExecuteAutomationActionCommand" in handler
    assert 'scope="automation:script:runtime"' in handler
    assert "action_executor(action.key)" in handler


def test_script_publication_redirect_and_workflow_guidance_are_complete() -> None:
    route = _source("app/web/admin/automation_center.py")
    guidance = _source("docs/ADMIN_WORKFLOW_GUIDANCE.md")
    assert "notice: str | None = None" in route
    assert "automation-script-publish:" in route
    assert "typed `actions`" in guidance


def test_script_publication_and_client_delivery_are_governed() -> None:
    scripts = _source("app/services/automation_scripts.py")
    client_runtime = _source("static/js/automation-client-runtime.js")
    web = _source("app/web/admin/automation_center.py")
    assert "runtime_unavailable" in scripts
    assert "_validate_source" in scripts
    assert "content_sha256" in client_runtime
    assert "published_client_scripts" in web
    assert "target.read_permission" in web
