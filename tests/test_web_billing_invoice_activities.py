from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.system_user import SystemUser
from app.services import web_billing_invoices
from app.services.web_billing_invoices import build_invoice_activities


def test_invoice_activity_uses_staff_name_and_hides_paired_system_closure(
    db_session,
):
    invoice_id = str(uuid4())
    closure_id = str(uuid4())
    staff = SystemUser(
        first_name="Confidence",
        last_name="Okaka",
        email="confidence.okaka@example.test",
    )
    db_session.add(staff)
    db_session.flush()
    db_session.add_all(
        [
            AuditEvent(
                actor_type=AuditActorType.system,
                action="void",
                entity_type="invoice",
                entity_id=invoice_id,
                metadata_={"closure_id": closure_id},
            ),
            AuditEvent(
                actor_type=AuditActorType.user,
                actor_id=str(staff.id),
                action="void",
                entity_type="invoice",
                entity_id=invoice_id,
                metadata_={"closure_id": closure_id},
            ),
            AuditEvent(
                actor_type=AuditActorType.system,
                action="create_invoice_draft",
                entity_type="invoice",
                entity_id=invoice_id,
            ),
        ]
    )
    db_session.flush()

    activities = build_invoice_activities(db_session, invoice_id=invoice_id)

    assert len(activities) == 2
    void_activity = next(item for item in activities if item["title"] == "Void")
    system_activity = next(
        item for item in activities if item["title"] == "Create Invoice Draft"
    )
    assert void_activity["description"] == "Confidence Okaka"
    assert system_activity["description"] == "System"


@pytest.mark.parametrize(
    ("service_command", "web_command"),
    [
        ("confirm_void", "confirm_invoice_void_web"),
        ("confirm_write_off", "confirm_invoice_write_off_web"),
    ],
)
def test_invoice_closure_web_records_only_the_attributed_audit_event(
    monkeypatch,
    service_command: str,
    web_command: str,
):
    captured: dict[str, object] = {}

    def fake_confirm(*args, **kwargs):
        captured["command_kwargs"] = kwargs
        return SimpleNamespace(closure=SimpleNamespace(id=uuid4()))

    def fake_log_audit_event(**kwargs):
        captured["audit_kwargs"] = kwargs

    monkeypatch.setattr(
        web_billing_invoices.billing_service.invoices,
        service_command,
        fake_confirm,
    )
    monkeypatch.setattr(
        web_billing_invoices,
        "log_audit_event",
        fake_log_audit_event,
    )

    getattr(web_billing_invoices, web_command)(
        object(),
        request=object(),
        actor_id="staff-user-id",
        invoice_id=str(uuid4()),
        preview_fingerprint="preview",
        idempotency_key="request-key",
        memo=None,
    )

    assert captured["command_kwargs"] == {"stage_audit": False}
    assert captured["audit_kwargs"]["actor_id"] == "staff-user-id"
