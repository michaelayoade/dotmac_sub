from pathlib import Path
from uuid import uuid4

from fastapi.routing import APIRoute

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.services.workqueue import ItemKind, WorkqueuePrincipal
from app.services.workqueue.web import build_page
from app.web.admin import workqueue as workqueue_web
from tests.staff_identity_fixtures import add_bound_staff_user


def test_workqueue_web_replaces_all_seven_crm_route_behaviors():
    routes = {
        (route.path, frozenset(route.methods))
        for route in workqueue_web.router.routes
        if isinstance(route, APIRoute)
    }

    assert ("/workqueue", frozenset({"GET"})) in routes
    assert ("/workqueue/_right-now", frozenset({"GET"})) in routes
    assert ("/workqueue/_section/{kind}", frozenset({"GET"})) in routes
    assert ("/workqueue/snooze", frozenset({"POST"})) in routes
    assert ("/workqueue/snooze/clear", frozenset({"POST"})) in routes
    assert ("/workqueue/claim", frozenset({"POST"})) in routes
    assert ("/workqueue/complete", frozenset({"POST"})) in routes


def test_workqueue_forms_keep_csrf_and_idempotency_evidence():
    root = Path("templates/admin/workqueue")
    row = (root / "_row.html").read_text(encoding="utf-8")
    index = (root / "index.html").read_text(encoding="utf-8")
    action_form = Path("templates/components/forms/action_form.html").read_text(
        encoding="utf-8"
    )

    assert row.count("components/forms/csrf_input.html") == 2
    assert row.count('name="request_id"') == 2
    assert 'action="/admin/workqueue/snooze"' in row
    assert 'action="/admin/workqueue/snooze/clear"' in row
    assert (
        'from "components/forms/action_form.html" import action_form with context'
        in row
    )
    assert "action_form(row.claim_action)" in row
    assert "action_form(row.complete_action)" in row
    assert "components/forms/csrf_input.html" in action_form
    assert 'name="{{ hidden.key }}"' in action_form
    assert 'name="confirmed"' in action_form
    assert "workqueue-refresh from:body" in index
    assert "/api/v1/workqueue/events" in index
    assert 'addEventListener("workqueue_changed"' in index
    assert "Generated {{ projection.generated_at_label }}" in (
        root / "_right_now.html"
    ).read_text(encoding="utf-8")


def test_workqueue_cards_use_responsive_three_column_grids():
    root = Path("templates/admin/workqueue")
    right_now = (root / "_right_now.html").read_text(encoding="utf-8")
    section = (root / "_section.html").read_text(encoding="utf-8")
    row = (root / "_row.html").read_text(encoding="utf-8")

    responsive_grid = "grid gap-3 md:grid-cols-2 xl:grid-cols-3"
    assert responsive_grid in right_now
    assert responsive_grid in section
    assert "flex h-full min-w-0 flex-col" in row
    assert "lg:grid-cols-[" not in row


def test_workqueue_cards_use_open_as_the_only_source_link():
    row = Path("templates/admin/workqueue/_row.html").read_text(encoding="utf-8")

    assert '<a href="{{ row.url }}" class="mt-2' not in row
    assert '<div class="mt-1.5 truncate text-[15px] font-semibold' in row
    assert row.count('href="{{ row.url }}"') == 1
    assert ">Open</a>" in row


def test_workqueue_section_navigation_switches_accessible_panels_without_reload():
    index = Path("templates/admin/workqueue/index.html").read_text(encoding="utf-8")

    for target in (
        "#workqueue-right-now",
        "#workqueue-section-conversation",
        "#workqueue-section-ticket",
        "#workqueue-section-project",
        "#workqueue-section-work_order",
    ):
        assert f'href="{target}"' in index
    assert 'aria-label="Workqueue sections"' in index
    assert 'role="tablist"' in index
    assert index.count('role="tab"') == 5
    assert index.count('role="tabpanel"') == 2
    assert "showSection(link);" in index
    assert "panel.hidden = !isActive" in index
    assert 'link.setAttribute("aria-selected", String(isActive))' in index
    assert "history.replaceState" in index
    assert 'window.addEventListener("hashchange"' in index
    assert "scrollIntoView" not in index
    assert 'panel.classList.add("workqueue-panel-enter")' in index
    assert "@keyframes workqueue-card-enter" in index
    assert "prefers-reduced-motion: reduce" in index


def test_workqueue_navigation_tools_are_client_side_and_default_to_compact():
    root = Path("templates/admin/workqueue")
    index = (root / "index.html").read_text(encoding="utf-8")
    section = (root / "_section.html").read_text(encoding="utf-8")
    table_row = (root / "_row_table.html").read_text(encoding="utf-8")

    assert 'id="workqueue-search"' in index
    assert 'data-density="compact"' in index
    assert 'data-density="list"' in index
    assert "const PAGE_SIZE = 21" in index
    assert index.count('id="workqueue-page-previous"') == 1
    assert index.count('id="workqueue-page-next"') == 1
    assert index.count('id="workqueue-page-summary"') == 1
    assert 'let density = "compact"' in index
    assert "localStorage" not in index
    assert "workqueue-inspect" not in index
    assert "workqueue-inspect" not in (root / "_row.html").read_text(encoding="utf-8")
    assert "workqueue-inspect" not in table_row
    assert 'id="workqueue-back-top"' in index
    assert 'data-workqueue-layout="compact"' in section
    assert 'data-workqueue-layout="list"' in section
    assert "<table" in section
    assert all(
        label in section for label in ("Item", "Why now", "State / timing", "Actions")
    )
    assert "<tr" in table_row


def test_web_projection_exposes_identity_state_urgency_owner_hint_and_next_action(
    db_session,
):
    actor_id = uuid4()
    user, person = add_bound_staff_user(db_session, system_user_id=actor_id)
    team = ServiceTeam(name="Support")
    db_session.add(team)
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    ticket = Ticket(
        title="Customer offline",
        status=TicketStatus.open.value,
        priority="urgent",
        service_team_id=team.id,
    )
    db_session.add(ticket)
    db_session.flush()
    principal = WorkqueuePrincipal(
        person_id=user.id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=True,
    )

    projection = build_page(db_session, principal)
    row = next(
        row
        for section in projection.sections
        for row in section.rows
        if row.item_id == ticket.id
    )

    assert row.title == "Customer offline"
    assert row.status_label == "Open"
    assert row.urgency_label
    assert row.reason_label
    assert row.url == f"/admin/support/tickets/{ticket.id}"
    assert row.can_claim is True
    assert row.claim_action is not None
    assert row.complete_action is not None
    claim_hidden = {value.key: value.value for value in row.claim_action.hidden_values}
    assert claim_hidden["item_kind"] == ItemKind.ticket.value
    assert claim_hidden["item_id"] == str(ticket.id)
    assert claim_hidden["request_id"]
    assert claim_hidden["state_fingerprint"]
    assert row.complete_action.confirmation is not None
    assert projection.generated_at_label.endswith("WAT")
