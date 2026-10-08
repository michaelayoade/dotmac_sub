from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_workflow_builder_controls_have_visible_capability_states_and_serialization() -> (
    None
):
    template = _source("templates/admin/automation/rule_builder.html")

    assert "conditionButton.disabled = !conditionsAvailable" in template
    assert "actionButton.disabled = !actionsAvailable" in template
    assert "conditions-availability" in template
    assert "actions-availability" in template
    assert "conditionButton?.addEventListener" in template
    assert "actionButton?.addEventListener" in template
    assert "row.remove()" in template
    assert 'name="conditions_json"' in template
    assert 'name="actions_json"' in template
    assert "JSON.stringify(conditions)" in template
    assert "JSON.stringify(actions)" in template


def test_condition_operator_selection_and_system_lookups_are_preserved() -> None:
    template = _source("templates/admin/automation/rule_builder.html")
    route = _source("app/web/admin/automation_center.py")
    contracts = _source("app/services/automation_contracts.py")
    lookups = _source("app/services/automation_condition_lookups.py")

    assert (
        'row.addEventListener("change", (event) => { if (event.target === operator)'
        in template
    )
    assert "initial.operator = operator.value" in template
    assert 'operator.value = ""; refresh()' in template
    assert "window.initTypeaheadFields" in template
    assert '"lookup_key": field.lookup_key.value' in route
    assert '"/condition-options/{lookup_key}"' in route
    assert "class AutomationLookupKey" in contracts
    assert "value_exists" in lookups


def test_automation_email_action_commits_through_notification_owner() -> None:
    actions = _source("app/services/automation_actions.py")
    assert "Notifications.create_internal_notification" in actions
    assert "queue_staff_notification" not in actions


def test_focused_workspaces_and_lifecycle_actions_are_reachable() -> None:
    route = _source("app/web/admin/automation_center.py")
    hub = _source("templates/admin/automation/index.html")
    workflows = _source("templates/admin/automation/workflows.html")
    scripts = _source("templates/admin/automation/script_list.html")

    for path in (
        '"/workflows"',
        '"/client-scripts/manage"',
        '"/server-scripts"',
        '"/runs"',
    ):
        assert path in route or path.replace('"', "") in hub
    assert 'href="/admin/automation/workflows"' in hub
    assert 'href="/admin/automation/client-scripts/manage"' in hub
    assert 'href="/admin/automation/server-scripts"' in hub
    assert 'href="/admin/automation/runs"' in hub
    for action in ("publish", "pause", "resume"):
        assert (
            f"/admin/automation/rules/{{{{ workflow.rule_id }}}}/{action}" in workflows
        )
    assert "/admin/automation/scripts/{{ script.script_id }}/publish" in scripts
    assert "return_to" in route


def test_workflow_workspace_keeps_filters_aligned_and_actions_visible() -> None:
    workflows = _source("templates/admin/automation/workflows.html")

    assert 'class="flex flex-nowrap items-center gap-3 overflow-x-auto' in workflows
    assert 'class="min-w-64 flex-1"' in workflows
    assert 'class="w-44 shrink-0"' in workflows
    assert 'class="w-56 shrink-0"' in workflows
    assert "border border-white bg-primary-600" in workflows
    assert "border border-slate-400 bg-white" in workflows
    assert "border border-primary-700 bg-primary-600" in workflows


def test_customer_and_support_event_options_are_owner_declared() -> None:
    customer = _source("app/services/sot_registry/domains/customer_context.py")
    support = _source("app/services/sot_registry/domains/support_operations.py")
    support_owner = _source("app/services/support.py")

    for event in (
        "customer.account.created",
        "customer.account.updated",
        "customer.account.status_changed",
        "customer.account.suspended",
        "customer.account.reactivated",
    ):
        assert event in customer
    for event in (
        "support.ticket.created",
        "support.ticket.assigned",
        "support.ticket.status_changed",
        "support.ticket.priority_changed",
        "support.ticket.resolution_requested",
        "support.ticket.resolution_confirmed",
        "support.ticket.resolution_disputed",
    ):
        assert event in support
    assert '"ticket.status_changed"' in support_owner
    assert '"ticket.priority_changed"' in support_owner
    assert "cannot be invented in this form" in _source(
        "templates/admin/automation/rule_builder.html"
    )


def test_script_mechanisms_explain_their_distinct_execution_boundaries() -> None:
    template = _source("templates/admin/automation/script_builder.html")
    assert "Both mechanisms use JavaScript" in template
    assert "restricted get/set/error API" in template
    assert "isolated JavaScript worker" in template
    assert "stable lowercase dotted identifier" in template
    assert "no free-text event entry" in template
