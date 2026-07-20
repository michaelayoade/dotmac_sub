from pathlib import Path

from app.models.catalog import DunningAction
from app.schemas.collections import DunningActionLogCreate, DunningCaseCreate
from app.services import collections as collections_service
from app.services import web_billing_dunning
from app.web.admin import billing_dunning as dunning_routes
from app.web.admin.billing_dunning import router


def test_dunning_detail_route_is_registered():
    routes = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", []))))
        for route in router.routes
    }

    assert ("/billing/dunning/{case_id}", ("GET",)) in routes


def test_dunning_detail_data_includes_case_account_and_actions(
    db_session, subscriber_account
):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(account_id=subscriber_account.id),
    )
    action = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent",
        ),
    )

    state = web_billing_dunning.build_detail_data(db_session, case_id=str(case.id))

    assert state["case"].id == case.id
    assert state["account"].id == subscriber_account.id
    assert state["actions"][0].id == action.id


def test_dunning_templates_link_to_real_detail_route():
    listing = Path("templates/admin/billing/dunning.html").read_text()
    detail = Path("templates/admin/billing/dunning_detail.html").read_text()

    assert 'href="/admin/billing/dunning/{{ case.id }}"' in listing
    assert "Action History" in detail
    assert "Pause Case" in detail
    assert "Close Case" in detail
    assert "dunning_note" in listing
    assert "dunning_warning" in listing


def test_dunning_bulk_pause_redirect_reports_partial_success(db_session, monkeypatch):
    def _fake_actor_id(request):
        return "actor-1"

    def _fake_execute_bulk_action_with_audit_result(
        db, *, request, action, actor_id, case_id=None, case_ids_csv=None
    ):
        assert action == "pause"
        assert actor_id == "actor-1"
        assert case_ids_csv == "case-1,case-2,missing"
        assert case_id is None
        return web_billing_dunning.BulkDunningActionResult(
            selected_ids=["case-1", "case-2", "missing"],
            processed_ids=["case-1"],
            failed_ids=["missing"],
        )

    monkeypatch.setattr(dunning_routes, "_actor_id", _fake_actor_id)
    monkeypatch.setattr(
        dunning_routes.web_billing_dunning_service,
        "execute_bulk_action_with_audit_result",
        _fake_execute_bulk_action_with_audit_result,
    )

    response = dunning_routes.dunning_bulk_pause(
        request=None,
        case_ids="case-1,case-2,missing",
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/admin/billing/dunning?dunning_note="
    )
    assert "dunning_warning=" in response.headers["location"]
