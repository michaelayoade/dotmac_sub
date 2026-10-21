"""Transport guards preserve original reviewed revision and intent."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.web.admin import field_visit_followups as web


def test_invalid_csrf_before_owner(monkeypatch):
    monkeypatch.setattr(
        web, "parse_form_data_sync", lambda request: {"_csrf_token": "wrong"}
    )
    access = PermissionAdmission(uuid4(), "system_user", frozenset())
    with pytest.raises(HTTPException) as caught:
        web._submit(
            SimpleNamespace(cookies={web.CSRF_COOKIE_NAME: "correct"}),
            None,
            access,
            access,
            access,
        )
    assert caught.value.status_code == 403


def test_stale_original_revision_intent_and_input_retained(monkeypatch):
    values = {
        "_csrf_token": "match",
        "intent_key": str(uuid4()),
        "revision_id": str(uuid4()),
        "team_id": str(uuid4()),
        "person_id": "",
        "due_at": "2026-10-12T09:00:00+01:00",
        "reason": "Reviewed no-access followup",
        "disposition": "revisit_required",
    }
    monkeypatch.setattr(web, "parse_form_data_sync", lambda request: values)
    monkeypatch.setattr(
        web.db_session_adapter, "release_read_transaction", lambda db: None
    )
    captured = {}

    def stale(db, *, command):
        captured["command"] = command
        raise DomainError(
            code="operations.field_visit_followup.stale_revision",
            message="Reload evidence",
        )

    monkeypatch.setattr(web.owner, "decide_followup", stale)
    monkeypatch.setattr(web, "_render", lambda *args, **kwargs: kwargs)
    access = PermissionAdmission(uuid4(), "system_user", frozenset())
    result = web._submit(
        SimpleNamespace(cookies={web.CSRF_COOKIE_NAME: "match"}),
        None,
        access,
        access,
        access,
        obligation_id=uuid4(),
    )
    assert result["status"] == 409
    assert result["submitted"]["revision_id"] == values["revision_id"]
    assert result["submitted"]["intent_key"] == values["intent_key"]
    assert str(captured["command"].expected_revision_id) == values["revision_id"]
    assert result["submitted"]["due_at"] == values["due_at"]


def test_template_compiles_and_replacement_route_requires_explicit_write():
    Environment(autoescape=True, loader=FileSystemLoader("templates")).get_template(
        "admin/dispatch/visit_followups.html"
    )
    import inspect

    signature = inspect.signature(web.followup_link_replacement)
    dependency = signature.parameters["replacement"].default
    assert dependency.dependency is not None


@pytest.mark.parametrize("detail", [False, True])
def test_work_order_links_route_by_sub_public_id(detail):
    from datetime import UTC, datetime

    work_order_id = uuid4()
    source = SimpleNamespace(
        kind=SimpleNamespace(value="unable_event"), source_id=uuid4()
    )
    revision = SimpleNamespace(
        sequence=1,
        disposition=SimpleNamespace(value="triage"),
        decided_at=datetime.now(UTC),
        responsible_team_label="Triage",
        responsible_person_label=None,
        due_at=datetime.now(UTC),
        reason="Reviewed failure",
        replacement_decision_id=None,
    )
    workspace = (
        SimpleNamespace(
            obligation_id=uuid4(),
            work_order_id=work_order_id,
            work_order_public_id="sub-followup-routing",
            source=source,
            revisions=(revision,),
            readiness=SimpleNamespace(state=SimpleNamespace(value="blocked")),
        )
        if detail
        else None
    )
    row = SimpleNamespace(
        work_order_id=work_order_id,
        work_order_public_id="sub-followup-routing",
        work_order_label="Failed visit",
        source=source,
        occurred_at=datetime.now(UTC),
        failure_reason="No access",
        readiness=SimpleNamespace(state=SimpleNamespace(value="blocked")),
    )
    template = Environment(
        autoescape=True, loader=FileSystemLoader("templates")
    ).get_template("admin/dispatch/visit_followups.html")
    context = template.new_context(
        {
            "request": SimpleNamespace(query_params={}),
            "workspace": workspace,
            "pending": SimpleNamespace(rows=(row,), next_work_order_id=None),
            "queue": SimpleNamespace(rows=(), next_obligation_id=None),
            "submitted": {},
            "selected_team": "",
            "options": SimpleNamespace(teams=(), people=()),
            "form_contract": web.FORM,
        }
    )
    html = "".join(template.blocks["content"](context))
    assert 'href="/admin/dispatch/work-orders/sub-followup-routing"' in html
    assert f"/admin/dispatch/work-orders/{work_order_id}" not in html


def _render_block(**updates):
    context = {
        "request": SimpleNamespace(query_params={}),
        "workspace": None,
        "pending": SimpleNamespace(rows=(), next_work_order_id=None),
        "queue": SimpleNamespace(rows=(), next_obligation_id=None),
        "submitted": {},
        "selected_team": "",
        "options": SimpleNamespace(teams=(), people=()),
        "selected_source": None,
        "form_contract": web.FORM,
        "approved_link_page": False,
    }
    context.update(updates)
    template = Environment(
        autoescape=True, loader=FileSystemLoader("templates")
    ).get_template("admin/dispatch/visit_followups.html")
    return "".join(template.blocks["content"](template.new_context(context)))


def test_history_pagination_keeps_history_mode():
    cursor = uuid4()
    html = _render_block(
        request=SimpleNamespace(query_params={"history": "1"}),
        queue=SimpleNamespace(rows=(), next_obligation_id=cursor),
    )
    assert f"?after_followup={cursor}&amp;history=1" in html


@pytest.mark.parametrize("state,active", [("blocked", False), ("ready", True)])
def test_selected_source_form_uses_current_assignment_readiness(state, active):
    html = _render_block(
        request=SimpleNamespace(
            query_params={"source_id": str(uuid4()), "source_kind": "unable_event"}
        ),
        selected_source=SimpleNamespace(
            readiness=SimpleNamespace(state=SimpleNamespace(value=state))
        ),
    )
    assert ('method="post"' in html) == active
    assert ("Record followup review" in html) == active
    assert "Record with approved replacement linkage" not in html


def test_approved_choice_posts_exact_decision_and_generation(monkeypatch):
    values = {
        "_csrf_token": "match",
        "intent_key": str(uuid4()),
        "revision_id": str(uuid4()),
        "team_id": str(uuid4()),
        "due_at": "2026-10-12T09:00:00+01:00",
        "reason": "Reviewed exact approved edge",
        "disposition": "revisit_required",
        "replacement_decision_id": str(uuid4()),
        "replacement_generation": "3",
    }
    monkeypatch.setattr(web, "parse_form_data_sync", lambda request: values)
    monkeypatch.setattr(
        web.db_session_adapter, "release_read_transaction", lambda db: None
    )
    captured = {}

    def owner_command(db, *, command):
        captured["command"] = command
        raise DomainError(
            code="operations.field_visit_followup.stale_revision",
            message="Reload evidence",
        )

    monkeypatch.setattr(web.owner, "decide_followup", owner_command)
    monkeypatch.setattr(web, "_render", lambda *args, **kwargs: kwargs)
    access = PermissionAdmission(uuid4(), "system_user", frozenset())
    result = web._submit(
        SimpleNamespace(cookies={web.CSRF_COOKIE_NAME: "match"}),
        None,
        access,
        access,
        access,
        obligation_id=uuid4(),
        replacement_access=access,
    )
    assert (
        str(captured["command"].replacement_decision_id)
        == values["replacement_decision_id"]
    )
    assert captured["command"].expected_replacement_generation == 3
    assert result["submitted"]["replacement_generation"] == "3"
    assert result["replacement_access"] == access


def _workspace(replacement_state="blocked"):
    from datetime import UTC, datetime

    revision = SimpleNamespace(
        revision_id=uuid4(),
        sequence=1,
        disposition=SimpleNamespace(value="triage"),
        responsible_team_id=uuid4(),
        responsible_team_label="Triage",
        responsible_system_user_id=None,
        responsible_person_label=None,
        due_at=datetime.now(UTC),
        reason="Original failure",
        decided_at=datetime.now(UTC),
        replacement_decision_id=None,
    )
    return SimpleNamespace(
        obligation_id=uuid4(),
        work_order_id=uuid4(),
        work_order_public_id="sub-original",
        source=SimpleNamespace(
            kind=SimpleNamespace(value="unable_event"), source_id=uuid4()
        ),
        revisions=(revision,),
        readiness=SimpleNamespace(state=SimpleNamespace(value="ready")),
        replacement_readiness=SimpleNamespace(
            state=SimpleNamespace(value=replacement_state)
        ),
    )


@pytest.mark.parametrize("state,visible", [("blocked", False), ("ready", True)])
def test_linkage_navigation_requires_current_completion_write_readiness(state, visible):
    html = _render_block(workspace=_workspace(state))
    assert ("Review current approved replacement linkage" in html) == visible
    assert 'name="replacement_decision_id"' not in html
    assert "Record with approved replacement linkage" not in html


def test_approved_link_page_has_human_choice_without_global_uuid_picker():
    decision_id = uuid4()
    html = _render_block(
        workspace=_workspace("ready"),
        approved_link_page=True,
        approved_option=SimpleNamespace(
            decision_id=decision_id,
            generation=3,
            work_order_public_id="sub-replacement",
            work_order_title="Restore rooftop access",
        ),
    )
    assert "sub-replacement — Restore rooftop access (approval 3)" in html
    assert f'<option value="{decision_id}">' in html
    assert 'name="replacement_generation" value="3"' in html
    assert (
        'input class="block rounded border p-2" name="replacement_decision_id"'
        not in html
    )
