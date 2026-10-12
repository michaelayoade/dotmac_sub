"""Retired mutation API and preserved audit read/append contracts (fast unit lane)."""

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.audit import router
from app.api.deps import require_audit_auth
from app.models.audit import AuditEvent
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents


def test_delete_is_405_and_absent_from_openapi() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_audit_auth] = lambda: None
    with TestClient(app) as client:
        response = client.delete(f"/api/v1/audit-events/{uuid4()}")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    paths = app.openapi()["paths"]
    assert set(paths["/api/v1/audit-events/{event_id}"]) == {"get"}
    assert "get" in paths["/api/v1/audit-events"]
    assert not hasattr(AuditEvents, "delete")


def test_historical_inactive_rows_remain_readable(db_session: Session) -> None:
    active = AuditEvents.stage(
        db_session, AuditEventCreate(action="active", entity_type="audit-test")
    )
    historical = AuditEvents.stage(
        db_session,
        AuditEventCreate(
            action="historical", entity_type="audit-test", is_active=False
        ),
    )
    db_session.flush()
    assert AuditEvents.list(db_session) == [active]
    assert AuditEvents.list(db_session, is_active=True) == [active]
    assert AuditEvents.list(db_session, is_active=False) == [historical]
    assert AuditEvents.get(db_session, str(historical.id)) is historical


def test_stage_appends_without_completing_caller_transaction(
    db_session: Session,
) -> None:
    event = AuditEvents.stage(
        db_session,
        AuditEventCreate(
            action="append", entity_type="audit-test", metadata_={"reason": "test"}
        ),
    )
    db_session.flush()
    event_id = event.id
    assert event.details == {"reason": "test"}
    assert event.created_at is not None
    assert db_session.in_transaction()
    db_session.rollback()
    assert db_session.get(AuditEvent, event_id) is None
