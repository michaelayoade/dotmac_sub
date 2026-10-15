from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.system_user import SystemUser, SystemUserType
from app.services import audit as audit_service
from app.services import audit_helpers, web_system_audit


def test_audit_page_resolves_system_user_actor_names(db_session):
    user = SystemUser(
        first_name="Admin",
        last_name="Operator",
        email="admin-operator@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        AuditEvent(
            actor_type=AuditActorType.user,
            actor_id=str(user.id),
            action="update",
            entity_type="support_ticket",
            entity_id="ticket-1",
            is_success=True,
        )
    )
    db_session.commit()

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(
            entity_type="support_ticket",
            page=1,
        ),
    )

    assert page["events"][0]["actor_name"] == "Admin Operator"
    assert page["total"] == 1


def test_recent_activity_feed_resolves_system_user_actor_names(db_session):
    user = SystemUser(
        first_name="Audit",
        last_name="Reviewer",
        email="audit-reviewer@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id=str(user.id),
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        is_success=True,
    )
    db_session.add(event)
    db_session.commit()

    feed = audit_helpers.build_recent_activity_feed(db_session, [event], limit=5)

    assert feed[0]["message"].startswith("Audit Reviewer ")


def test_recent_activity_feed_uses_actor_name_metadata_for_unresolved_user(
    db_session,
):
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id="missing-user-id",
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        is_success=True,
        metadata_={"actor_name": "Archived Admin"},
    )
    db_session.add(event)
    db_session.commit()

    feed = audit_helpers.build_recent_activity_feed(db_session, [event], limit=5)

    assert feed[0]["message"].startswith("Archived Admin ")


def test_log_audit_event_derives_system_user_actor_name_when_id_missing(db_session):
    user = SystemUser(
        first_name="NOC",
        last_name="Admin",
        email="noc-admin@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            user=user,
            auth={"principal_type": "system_user", "principal_id": str(user.id)},
        ),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )

    audit_helpers.log_audit_event(
        db=db_session,
        request=request,
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        actor_id="",
    )

    event = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == "inv-1",
            AuditEvent.action == "update",
        )
        .one()
    )
    assert event.actor_type == AuditActorType.user
    assert event.actor_id == str(user.id)
    assert event.metadata_["actor_name"] == "NOC Admin"
    assert event.metadata_["actor_email"] == "noc-admin@example.com"

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(page=1),
    )

    assert page["events"][0]["actor_name"] == "NOC Admin"


# --- Audit list-projection contract (ui.audit_events_list_projection) ---


def test_audit_list_definition_declares_expected_capabilities():
    definition = web_system_audit.AUDIT_EVENTS_LIST_DEFINITION
    assert definition.filterable_keys == (
        "actor_id",
        "actor_type",
        "action",
        "entity_type",
        "result",
        "period",
        "date_from",
        "date_to",
        "entity_id",
        "entity_id_missing",
        "request_id",
        "request_id_missing",
    )
    assert definition.sortable_keys == ("occurred_at",)
    assert definition.default_sort == "occurred_at"
    assert definition.default_sort_dir == "desc"
    assert definition.default_per_page == 50


def test_audit_results_do_not_scroll_horizontally():
    source = Path("templates/admin/system/_audit_table.html").read_text(
        encoding="utf-8"
    )

    assert '<div class="overflow-hidden">' in source
    assert '<table class="w-full table-fixed' in source
    assert "overflow-x-auto" not in source
    assert "whitespace-nowrap" not in source


def test_audit_table_labels_action_and_module_columns_correctly():
    source = Path("templates/admin/system/_audit_table.html").read_text(
        encoding="utf-8"
    )

    headings = '["Time", "Actor", "Action", "Module", "Details", "Status"]'
    assert headings in source
    assert "event.action_label" in source
    assert "event.entity_type" in source
    assert 'colspan="6"' in source


def test_build_audit_list_query_normalizes_and_defaults():
    query = web_system_audit.build_audit_list_query(
        actor_id=" ",
        actor_type="service",
        action="update",
        entity_type="invoice",
        result="failed",
        period="last_7_days",
        page=2,
    )
    # Blank filters dropped; supplied ones kept.
    assert query.filter_value("actor_id") is None
    assert query.filter_value("action") == "update"
    assert query.filter_value("actor_type") == "service"
    assert query.filter_value("entity_type") == "invoice"
    assert query.filter_value("result") == "failed"
    assert query.filter_value("period") == "last_7_days"
    assert query.sort_by == "occurred_at"
    assert query.sort_dir == "desc"
    assert query.page == 2
    assert query.per_page == 50


def test_build_audit_list_query_rejects_out_of_contract_params():
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(sort_by="actor_id")
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(per_page=20)
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(result="unknown")
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(
            entity_id="invoice-1", entity_id_missing=True
        )


def test_get_audit_page_data_filters_through_the_query(db_session):
    user = SystemUser(
        first_name="Filter",
        last_name="Target",
        email="filter-target@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    for action in ("create", "update"):
        db_session.add(
            AuditEvent(
                actor_type=AuditActorType.user,
                actor_id=str(user.id),
                action=action,
                entity_type="support_ticket",
                entity_id=f"ticket-{action}",
                is_success=True,
            )
        )
    db_session.commit()

    query = web_system_audit.build_audit_list_query(action="update")
    page = web_system_audit.get_audit_page_data(db_session, query)

    assert page["total"] == 1
    assert page["events"][0]["action"] == "update"


def test_audit_owner_returns_rows_and_total_from_the_same_filter_scope(db_session):
    for action, success in (("create", True), ("update", False), ("update", True)):
        db_session.add(
            AuditEvent(
                actor_type=AuditActorType.system,
                action=action,
                entity_type="invoice",
                entity_id=f"invoice-{action}-{success}",
                is_success=success,
            )
        )
    db_session.commit()

    result = audit_service.audit_events.query_page(
        db_session,
        audit_service.AuditEventPageQuery(
            action="update",
            is_success=False,
            limit=1,
        ),
    )

    assert result.total == 1
    assert len(result.events) == 1
    assert result.events[0].action == "update"
    assert result.events[0].is_success is False


def test_audit_filter_options_come_from_recorded_values(db_session):
    db_session.add_all(
        (
            AuditEvent(
                actor_type=AuditActorType.system,
                action="invoice.issue",
                entity_type="invoice",
                is_success=True,
            ),
            AuditEvent(
                actor_type=AuditActorType.system,
                action="ticket.close",
                entity_type="support_ticket",
                is_success=True,
            ),
        )
    )
    db_session.commit()

    options = audit_service.audit_events.filter_options(
        db_session, audit_service.AuditEventFilterOptionsQuery()
    )

    assert "invoice.issue" in options.actions
    assert "ticket.close" in options.actions
    assert "invoice" in options.entity_types
    assert "support_ticket" in options.entity_types


def test_audit_owner_filters_missing_identifiers(db_session):
    db_session.add_all(
        (
            AuditEvent(
                actor_type=AuditActorType.system,
                action="create",
                entity_type="invoice",
                entity_id=None,
                request_id=None,
                is_success=True,
            ),
            AuditEvent(
                actor_type=AuditActorType.system,
                action="create",
                entity_type="invoice",
                entity_id="invoice-1",
                request_id="request-1",
                is_success=True,
            ),
        )
    )
    db_session.commit()

    result = audit_service.audit_events.query_page(
        db_session,
        audit_service.AuditEventPageQuery(
            entity_id_missing=True,
            request_id_missing=True,
        ),
    )

    assert result.total == 1
    assert result.events[0].entity_id is None
    assert result.events[0].request_id is None


def test_audit_page_projection_does_not_query_audit_rows_directly():
    source = Path("app/services/web_system_audit.py").read_text(encoding="utf-8")

    assert "from app.models.audit import AuditEvent" not in source
    assert "select(func.count())" not in source
    assert "audit_events.query_page" in source


def test_audit_filter_ui_uses_recorded_options_and_pushes_filter_urls():
    page_source = Path("templates/admin/system/audit.html").read_text(encoding="utf-8")
    table_source = Path("templates/admin/system/_audit_table.html").read_text(
        encoding="utf-8"
    )

    assert 'name="entity_type"' in page_source
    assert ">Module</label>" in page_source
    assert "{% for option in module_options %}" in page_source
    assert "{% for option in action_options %}" in page_source
    assert 'hx-push-url="true"' in page_source
    assert 'hx-include="#audit-filters [name]"' in table_source
    assert 'hx-push-url="true"' in table_source
    assert "page_meta.navigation" in table_source
    assert "page_meta.start_item" in table_source
    assert "page_meta.end_item" in table_source
    assert 'name="per_page"' in page_source


def test_audit_page_data_returns_canonical_numbered_pagination(db_session):
    for index in range(11):
        db_session.add(
            AuditEvent(
                actor_type=AuditActorType.system,
                action="pagination.test",
                entity_type="audit_pagination_test",
                entity_id=str(index),
                is_success=True,
            )
        )
    db_session.commit()

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(
            entity_type="audit_pagination_test",
            page=2,
            per_page=10,
        ),
    )

    page_meta = page["page_meta"]
    assert page_meta.page == 2
    assert page_meta.start_item == 11
    assert page_meta.end_item == 11
    assert page_meta.total_items == 11
    assert page_meta.navigation == (1, 2)
    assert len(page["events"]) == 1
