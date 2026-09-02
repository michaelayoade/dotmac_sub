from app.services.admin_workflow_guidance import (
    WORKFLOW_GUIDANCE,
    guidance_categories,
    guidance_for_path,
    search_guidance,
)
from scripts.architecture.workflow_guidance_gate import validation_errors


def test_every_guide_has_plain_language_content_and_a_route() -> None:
    assert len(WORKFLOW_GUIDANCE) >= 20
    for guide in WORKFLOW_GUIDANCE:
        assert guide.id
        assert guide.title
        assert guide.purpose
        assert guide.steps
        assert guide.route_prefixes
        assert all(route.startswith("/admin") for route in guide.route_prefixes)


def test_change_plan_guide_is_searchable_and_contextual() -> None:
    guide = guidance_for_path("/admin/catalog/subscriptions/123")
    assert guide is not None
    assert guide.id == "change-plan"
    assert "change-plan" in {article.id for article in search_guidance(query="plan")}
    assert "Subscriptions" in guidance_categories()


def test_workflow_change_without_guidance_update_fails_gate() -> None:
    assert validation_errors(
        (__import__("pathlib").PurePosixPath("app/web/admin/reports.py"),)
    )
    assert not validation_errors(
        (
            __import__("pathlib").PurePosixPath("app/web/admin/reports.py"),
            __import__("pathlib").PurePosixPath(
                "app/services/admin_workflow_guidance.py"
            ),
        )
    )


def test_support_ticket_guidance_separates_editing_from_assignment() -> None:
    guide = guidance_for_path("/admin/support/tickets/123")

    assert guide is not None
    assert guide.id == "support-tickets"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "ordinary ticket editing" in content
    assert "ticket-assignment permission" in content
    assert "can still edit ordinary ticket details" in content
