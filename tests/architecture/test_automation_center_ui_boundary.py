from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_hub_route_is_permission_gated_and_admits_only_the_draft_pilot() -> None:
    source = _source("app/web/admin/automation_center.py")
    assert 'require_permission("automation:hub:read")' in source
    assert "@router.get" in source
    assert "@router.post" in source
    assert "RULE_READ_PERMISSION" in source
    assert "RULE_CREATE_PERMISSION" in source
    assert "RUN_READ_PERMISSION" in source
    assert '"/ticket-assignment/drafts"' in source
    assert '"support:ticket:update"' in source


def test_hub_is_registered_and_visible_only_with_hub_permission() -> None:
    routes = _source("app/web/admin/__init__.py")
    navigation = _source("templates/components/navigation/admin_sidebar.html")
    assert "router.include_router(automation_center_router)" in routes
    assert 'permission="automation:hub:read"' in navigation
    assert 'href="/admin/automation"' not in navigation
    assert '"/admin/automation"' in navigation


def test_hub_presents_governance_and_honest_dormant_state() -> None:
    template = _source("templates/admin/automation/index.html")
    for heading in (
        "Module registry",
        "Central rules",
        "Recent execution",
        "Existing automation ownership",
        "Runtime dormant",
    ):
        assert heading in template
    assert "Deployment creates no rules and causes no business side effects" in template
    assert "New ticket-assignment draft" in template


def test_ticket_assignment_draft_form_is_explicitly_non_executable() -> None:
    template = _source("templates/admin/automation/ticket_assignment_draft.html")
    assert "A new support ticket is created." in template
    assert "Ticket priority is" in template
    assert "Assign service team" in template
    assert "Saving creates a draft only" in template


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
