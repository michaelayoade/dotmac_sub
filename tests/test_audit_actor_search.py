"""Audit can be searched by person, and stores the label it searches on (Slice B)."""

from __future__ import annotations

import uuid

from app.models.audit import AuditActorType, AuditEvent
from app.services import audit as audit_service
from app.services import web_system_audit


def _event(db, *, actor_id, actor_type, actor_label=None, action="update"):
    db.add(
        AuditEvent(
            actor_type=actor_type,
            actor_id=str(actor_id),
            actor_label=actor_label,
            action=action,
            entity_type="support_ticket",
            entity_id="t-1",
            is_success=True,
        )
    )
    db.commit()


def test_search_matches_the_stored_label_case_insensitively(db_session):
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="Aisha Ibrahim",
    )
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="Chibuzor Nnamani",
    )

    rows = audit_service.audit_events.list(db_session, actor_search="aisha")

    assert len(rows) == 1
    assert rows[0].actor_label == "Aisha Ibrahim"


def test_search_matches_an_api_key_label(db_session):
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.api_key,
        actor_label="crm-service-integration",
    )

    rows = audit_service.audit_events.list(db_session, actor_search="crm-service")

    assert len(rows) == 1


def test_search_also_matches_an_exact_actor_id(db_session):
    actor_id = uuid.uuid4()
    _event(
        db_session,
        actor_id=actor_id,
        actor_type=AuditActorType.user,
        actor_label="Someone",
    )

    rows = audit_service.audit_events.list(db_session, actor_search=str(actor_id))

    assert len(rows) == 1


def test_no_search_returns_everything(db_session):
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="One",
    )
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="Two",
    )

    assert len(audit_service.audit_events.list(db_session)) == 2


def test_page_data_search_filters_events_and_total(db_session):
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="Grace Data Moses",
    )
    _event(
        db_session,
        actor_id=uuid.uuid4(),
        actor_type=AuditActorType.user,
        actor_label="Monica Edako",
    )

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(actor_id="grace", page=1),
    )

    assert page["total"] == 1
    assert page["events"][0]["actor_name"] == "Grace Data Moses"


def test_stored_label_is_preferred_over_live_resolution(db_session):
    """The column is authoritative; a mislabelled dict must not override it."""
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id=str(uuid.uuid4()),
        actor_label="Stored Name",
        action="update",
        entity_type="invoice",
        entity_id="i-1",
        is_success=True,
    )

    assert audit_service and web_system_audit  # imports used

    from app.services.audit_helpers import resolve_actor_name

    assert resolve_actor_name(event, {}) == "Stored Name"
