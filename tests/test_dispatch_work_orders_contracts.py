"""Dispatch work-order queue tiles and row actions are UI projection contracts.

The summary tiles come back as ``Kpi`` objects whose ``cohort_url`` filters the
work-order list to exactly the rows the tile counts (KPI-parity), and the
per-row queue button is an ``Action`` whose eligibility is owned by the
assignment transition command — never re-derived from the status string in the
template.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData

from app.models.work_order import WorkOrder
from app.services import web_dispatch_work_orders as service
from app.services.ui_contracts import Action, Kpi, StateValue
from app.web.admin import dispatch_work_orders as routes
from app.web.admin.dispatch_work_orders import (
    WorkOrderCreateFormValues,
    _redirect,
    templates,
)

_COUNTS = {
    "total": 12,
    "active": 7,
    "scheduled": 3,
    "in_progress": 2,
    "completed": 5,
}


def test_summary_tiles_are_kpi_contracts_carrying_state():
    kpis = service._work_order_kpis(_COUNTS)

    assert set(kpis) == {"total", "active", "scheduled", "in_progress", "completed"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert all(isinstance(kpi.value, StateValue) for kpi in kpis.values())
    # The headline shows the counted figure, never a zero standing in for it.
    assert kpis["active"].value.value == 7
    assert kpis["active"].value.is_present


def test_each_tile_drills_into_exactly_the_cohort_it_counts():
    kpis = service._work_order_kpis(_COUNTS)

    for kpi in kpis.values():
        assert kpi.cohort_url.startswith("/admin/dispatch/work-orders")

    # Total is the whole list; the status tiles narrow by a single status; the
    # open-work tile narrows to non-terminal rows via the active filter.
    assert kpis["total"].cohort_url == "/admin/dispatch/work-orders"
    assert (
        kpis["scheduled"].cohort_url == "/admin/dispatch/work-orders?status=scheduled"
    )
    assert (
        kpis["in_progress"].cohort_url
        == "/admin/dispatch/work-orders?status=in_progress"
    )
    assert (
        kpis["completed"].cohort_url == "/admin/dispatch/work-orders?status=completed"
    )
    assert kpis["active"].cohort_url == "/admin/dispatch/work-orders?active=1"


def test_queue_action_is_allowed_for_open_work_orders(db_session):
    from app.models.work_order import WorkOrder

    row = WorkOrder(
        public_id="qa-action-open", title="QA", is_active=True, status="scheduled"
    )
    db_session.add(row)
    db_session.flush()
    action = service._queue_action(db_session, row)

    assert isinstance(action, Action)
    assert action.key == "queue"
    assert action.allowed is True
    assert action.reason is None
    assert action.permission == "operations:dispatch:assign"


def test_queue_action_is_blocked_with_a_reason_for_terminal_or_inactive(db_session):
    from app.models.work_order import WorkOrder

    rows = [
        WorkOrder(
            public_id=f"qa-action-{status}-{active}",
            title="QA",
            is_active=active,
            status=status,
        )
        for status, active in (
            ("completed", True),
            ("canceled", True),
            ("scheduled", False),
        )
    ]
    db_session.add_all(rows)
    db_session.flush()
    terminal, canceled, inactive = [
        service._queue_action(db_session, row) for row in rows
    ]
    for action in (terminal, canceled, inactive):
        assert action.allowed is False
        assert action.reason
    assert "completed" in terminal.reason
    assert inactive.reason == "Work order is inactive"


def test_action_contract_rejects_an_allowed_action_that_carries_a_reason():
    with pytest.raises(ValueError):
        Action(key="queue", label="Queue", allowed=True, reason="nope")


def test_list_page_exposes_kpi_and_action_contracts(db_session):
    state = service.list_page(db_session)

    assert set(state["kpis"]) == {
        "total",
        "active",
        "scheduled",
        "in_progress",
        "completed",
    }
    assert all(isinstance(kpi, Kpi) for kpi in state["kpis"].values())
    assert all(isinstance(item["actions"]["queue"], Action) for item in state["items"])


def test_active_filter_narrows_the_list_to_non_terminal_work_orders(db_session):
    # The Active tile's cohort_url (?active=1) must resolve to a real filter so
    # the headline and the list it links to can never disagree.
    everything = service.list_page(db_session)
    active_only = service.list_page(db_session, active=True)

    assert active_only["active_filter"] is True
    assert all(
        item["work_order"].status not in ("completed", "canceled", "cancelled")
        for item in active_only["items"]
    )
    assert active_only["total"] <= everything["total"]


def test_template_consumes_the_kpi_and_action_contracts():
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/admin/dispatch/work_orders.html"
    ).read_text(encoding="utf-8")

    # Tiles render the StateValue and deep-link to the owner's cohort URL.
    assert "kpis.total.value.value" in source
    assert "href=kpis.active.cohort_url" in source
    assert "href=kpis.completed.cohort_url" in source
    assert source.count("stats_card(") == 4
    # Task-originated creation locks the validated subscriber/project/task scope.
    assert 'name="project_task_id"' in source
    assert "create_prefill.project_task_id" in source
    assert 'aria-controls="create-work-order-dialog"' in source
    assert '@click="createOpen = true;' in source
    assert 'id="create-work-order-dialog"' in source
    assert 'x-trap.inert.noscroll="createOpen"' in source
    assert "createTrigger?.focus()" in source
    assert '<details id="create-work-order"' not in source
    assert "Open form" not in source
    assert 'href="/admin/dispatch/work-orders/{{ wo.public_id }}"' in source
    assert 'name="assigned_technician_id"' not in source
    assert 'aria-label="Work order pages"' in source
    assert "previous_page_url" in source
    assert "next_page_url" in source


def test_shared_page_header_is_compact_and_unframed():
    html = templates.env.from_string(
        '{% from "components/ui/macros.html" import page_header %}'
        '{{ page_header("Dispatch", breadcrumbs=[{"label": "Dispatch"}]) }}'
    ).render()

    assert 'aria-label="Breadcrumb"' in html
    assert "rounded-3xl" not in html
    assert "bg-gradient" not in html
    assert "blur-3xl" not in html


def test_work_order_page_url_preserves_the_list_cohort():
    url = service._work_order_page_url(
        page=2,
        per_page=25,
        status="scheduled",
        q="fiber install",
        active=True,
        project_task_id="task-id",
    )

    assert parse_qs(urlsplit(url).query) == {
        "page": ["2"],
        "per_page": ["25"],
        "status": ["scheduled"],
        "q": ["fiber install"],
        "active": ["1"],
        "project_task_id": ["task-id"],
    }


def test_work_order_list_composes_with_one_header_form_trigger(db_session):
    request = SimpleNamespace(
        state=SimpleNamespace(
            csrf_token="test-csrf-token",
            csp_nonce="test-csp-nonce",
            auth={"permission_keys": {"*"}},
        ),
        query_params={},
        headers={},
        cookies={},
        url=SimpleNamespace(path="/admin/dispatch/work-orders"),
        session={},
        client=None,
        scope={},
    )
    context = service.list_page(db_session, can_create=True)
    context.update(
        {
            "request": request,
            "active_page": "dispatch-work-orders",
            "active_menu": "operations",
            "current_user": {
                "name": "Test Admin",
                "email": "admin@example.com",
            },
            "sidebar_stats": {},
            "notice": None,
            "error": None,
        }
    )

    html = templates.env.get_template("admin/dispatch/work_orders.html").render(
        **context
    )

    assert html.count('aria-controls="create-work-order-dialog"') == 1
    assert 'id="create-work-order-dialog"' in html
    assert "Open form" not in html
    assert '<details id="create-work-order"' not in html

    context.update(
        {
            "create_form_values": WorkOrderCreateFormValues.from_form(
                FormData(
                    [
                        ("subscriber_id", "subscriber-2"),
                        ("project_id", "project-2"),
                        ("title", "Repair east wing"),
                        ("work_type", "repair"),
                        ("priority", "high"),
                        ("scheduled_start", "2026-11-12T09:00"),
                        ("description", "Access via gate B"),
                    ]
                )
            ),
            "create_error": "Check the scheduled end time",
            "create_error_field": "scheduled_end",
        }
    )
    failed_html = templates.env.get_template("admin/dispatch/work_orders.html").render(
        **context
    )
    assert 'value="Repair east wing"' in failed_html
    assert 'value="2026-11-12T09:00"' in failed_html
    assert "Access via gate B" in failed_html
    assert 'value="repair" selected' in failed_html
    assert "Check the scheduled end time" in failed_html


def test_detail_template_owns_the_visible_assignment_next_action():
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/admin/dispatch/work_order_detail.html"
    ).read_text(encoding="utf-8")

    assert "Linked context" in source
    assert 'name="target_selection"' in source
    assert '<optgroup label="Vendors">' in source
    assert "Review assignment" in source
    assert "action_permitted(request, queue_action)" in source
    assert "/admin/support/tickets/{{ origin_ticket.id }}" in source


def test_post_create_redirect_identifies_the_new_work_order():
    response = _redirect(
        notice="Work order sub-new-work created",
        q="sub-new-work",
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)

    assert query == {
        "notice": ["Work order sub-new-work created"],
        "q": ["sub-new-work"],
    }


def test_failed_create_rerenders_the_submitted_form(monkeypatch):
    submitted = FormData(
        [("title", "Repair east wing"), ("scheduled_start", "bad date")]
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "permission_keys": {
                    "operations:dispatch:read",
                    "operations:dispatch:write",
                }
            }
        ),
        headers={},
    )
    captured = {}

    def reject_create(*args, **kwargs):
        raise ValueError("Invalid scheduled start")

    def render_form(*args, **kwargs):
        captured.update(kwargs)
        return "form response"

    monkeypatch.setattr(routes, "parse_form_data_sync", lambda request: submitted)
    monkeypatch.setattr(routes.work_orders_service, "create_from_form", reject_create)
    monkeypatch.setattr(
        routes.db_session_adapter, "discard_failed_transaction", lambda db: None
    )
    monkeypatch.setattr(routes, "_work_orders_response", render_form)

    assert routes.create_dispatch_work_order(request, db=object()) == "form response"
    assert captured["create_form"] is submitted
    assert captured["create_error"] == "Invalid scheduled start"


def test_failed_create_does_not_render_read_protected_rows_for_write_only_staff(
    monkeypatch,
):
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": {"operations:dispatch:write"}}),
        headers={},
    )
    monkeypatch.setattr(routes, "parse_form_data_sync", lambda request: FormData())
    monkeypatch.setattr(
        routes.work_orders_service,
        "create_from_form",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid title")),
    )
    monkeypatch.setattr(
        routes.db_session_adapter, "discard_failed_transaction", lambda db: None
    )
    monkeypatch.setattr(
        routes,
        "_work_orders_response",
        lambda *args, **kwargs: pytest.fail("read-protected list was rendered"),
    )
    with pytest.raises(HTTPException) as exc:
        routes.create_dispatch_work_order(request, db=object())
    assert exc.value.status_code == 422


def test_invalid_task_link_cannot_be_retried_as_an_unlinked_work_order(monkeypatch):
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "permission_keys": {
                    "operations:dispatch:read",
                    "operations:dispatch:write",
                }
            }
        ),
        headers={},
    )
    monkeypatch.setattr(
        routes,
        "parse_form_data_sync",
        lambda request: FormData([("project_task_id", "invalid-task")]),
    )
    monkeypatch.setattr(
        routes.work_orders_service,
        "create_from_form",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid task")),
    )
    monkeypatch.setattr(
        routes.db_session_adapter, "discard_failed_transaction", lambda db: None
    )
    with pytest.raises(HTTPException) as exc:
        routes.create_dispatch_work_order(request, db=object())
    assert exc.value.status_code == 409
    assert "Reopen the project task" in exc.value.detail


def test_stale_task_origin_stays_blocked_in_the_error_response(monkeypatch):
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "permission_keys": {
                    "operations:dispatch:read",
                    "operations:dispatch:write",
                }
            }
        )
    )
    task_id = "d0129727-784c-4f4e-a795-839cdfdf8f79"
    captured = {}

    def list_page(*args, **kwargs):
        captured["task_filter"] = kwargs["project_task_id"]
        return {
            "create_work_order_action": Action(
                key="create_work_order",
                label="Create Work Order",
                allowed=False,
                reason="Project subscriber is missing",
                permission="operations:dispatch:write",
            )
        }

    def render(template_name, context, *, status_code):
        captured["context"] = context
        captured["status_code"] = status_code
        return "blocked response"

    monkeypatch.setattr(routes.work_orders_service, "list_page", list_page)
    monkeypatch.setattr(routes, "_ctx", lambda request, db: {"request": request})
    monkeypatch.setattr(
        routes.web_custom_fields_service,
        "build_creation_form_context",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(routes.templates, "TemplateResponse", render)

    assert (
        routes._work_orders_response(
            request,
            db=object(),
            project_task_id=task_id,
            create_form=FormData([("project_task_id", task_id)]),
            create_error="Title is missing",
        )
        == "blocked response"
    )
    assert captured["task_filter"] == task_id
    assert captured["status_code"] == 422
    assert "Reopen the project task" in captured["context"]["error"]
    assert captured["context"]["create_error"] is None


def test_custom_field_multiselect_is_preserved_and_partial_creation_is_named(
    monkeypatch,
):
    submitted = FormData(
        [
            ("title", "Fiber visit"),
            ("custom_field__skills", "fiber"),
            ("custom_field__skills", "splicing"),
        ]
    )
    request = SimpleNamespace(state=SimpleNamespace(auth={}), headers={})
    row = WorkOrder(
        id=UUID("d0129727-784c-4f4e-a795-839cdfdf8f79"),
        public_id="sub-created",
    )
    captured = {}
    monkeypatch.setattr(routes, "parse_form_data_sync", lambda request: submitted)

    def create(*args, **kwargs):
        captured["create_input"] = kwargs["form"]
        return row

    def save_custom_fields(*args, **kwargs):
        captured["custom_form"] = kwargs["form"]
        raise ValueError("Invalid additional information")

    def redirect(public_id, **kwargs):
        captured["redirect_id"] = public_id
        captured.update(kwargs)
        return "detail response"

    monkeypatch.setattr(routes.work_orders_service, "create_from_form", create)
    monkeypatch.setattr(
        routes.web_custom_fields_service, "apply_creation_values", save_custom_fields
    )
    monkeypatch.setattr(routes, "_detail_redirect", redirect)

    assert routes.create_dispatch_work_order(request, db=object()) == "detail response"
    assert captured["create_input"].title == "Fiber visit"
    assert captured["custom_form"].getlist("custom_field__skills") == [
        "fiber",
        "splicing",
    ]
    assert captured["redirect_id"] == "sub-created"
    assert "was created" in captured["error"]
    assert "additional information was not saved" in captured["error"]
