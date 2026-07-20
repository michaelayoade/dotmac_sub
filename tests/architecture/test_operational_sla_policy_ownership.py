"""Pin operational SLA policy and event ownership to the generic owner."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/operational_escalation.py"


def test_only_operational_sla_owner_constructs_policy_rows() -> None:
    constructors: list[str] = []
    for path in (ROOT / "app/services").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "OperationalEscalationPolicy"
            for node in ast.walk(tree)
        ):
            constructors.append(str(path.relative_to(ROOT)))

    assert constructors == ["app/services/operational_escalation.py"]


def test_sla_ui_is_a_thin_adapter_over_the_owner() -> None:
    source = (ROOT / "app/services/web_notifications_sla_policies.py").read_text(
        encoding="utf-8"
    )
    routes = (ROOT / "app/web/admin/notifications.py").read_text(encoding="utf-8")
    form = (ROOT / "templates/admin/notifications/sla_policy_form.html").read_text(
        encoding="utf-8"
    )

    assert "operational_escalation.create_policy" in source
    assert "operational_escalation.update_policy" in source
    assert "operational_escalation.deactivate_policy_committed" in source
    assert "db.commit(" not in source
    assert '"/sla-policies"' in routes
    for field in ("entity_type", "trigger", "level", "delay_minutes", "channels"):
        assert f'name="{field}"' in form


def test_non_billing_breach_emitters_delegate_escalation_policy() -> None:
    ticket_source = (ROOT / "app/services/sla_assignment.py").read_text(
        encoding="utf-8"
    )
    project_source = (ROOT / "app/services/projects.py").read_text(encoding="utf-8")

    assert 'trigger="ticket.sla_breached"' in ticket_source
    assert "CUSTOMER_AND_CABINET_TICKET_TYPES_24H" not in ticket_source
    assert "CORE_LINK_TICKET_TYPES_48H" not in ticket_source
    assert 'trigger="project_task.sla_breached"' in project_source
    project_breach = project_source.split("def notify_project_task_sla_breach", 1)[
        1
    ].split("def _seed_fiber_installation_tasks", 1)[0]
    assert "_queue_email_notification" not in project_breach
    assert "_queue_in_app_notification" not in project_breach


def test_project_rule_names_the_generic_sla_owner() -> None:
    source = OWNER.read_text(encoding="utf-8")
    docs = (ROOT / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")
    normalized_docs = " ".join(docs.split())

    assert "def emit_sla_event" in source
    assert "operations.sla_escalation" in docs
    assert "A domain service may not embed a fallback SLA duration" in normalized_docs
