from starlette.requests import Request

from app.models.notification import NotificationChannel
from app.schemas.notification import NotificationTemplateCreate
from app.services import notification as notification_service
from app.web.admin import notifications as notifications_web


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_notification_template_test_uses_whatsapp_and_substitution(db_session, monkeypatch):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="WA Invoice",
            code="wa_invoice",
            channel=NotificationChannel.whatsapp,
            subject=None,
            body="Hello {{customer_name}}, amount due is {amount_due}",
        ),
    )
    captured = {}

    def _fake_send_text_message(*, db, recipient, body, dry_run):
        captured["recipient"] = recipient
        captured["body"] = body
        captured["dry_run"] = dry_run
        return {"ok": True, "response": "ok"}

    monkeypatch.setattr(
        "app.services.integrations.connectors.whatsapp.send_text_message",
        _fake_send_text_message,
    )

    response = notifications_web.notification_template_test(
        request=_request(),
        template_id=template.id,
        test_recipient="+2348000000000",
        test_variables_json='{"customer_name":"Ada","amount_due":"12500.00"}',
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["recipient"] == "+2348000000000"
    assert captured["dry_run"] is False
    assert captured["body"] == "Hello Ada, amount due is 12500.00"


def test_notification_template_preview_renders_variables(db_session):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="Email Invoice",
            code="email_invoice",
            channel=NotificationChannel.email,
            subject="Invoice {{invoice_number}}",
            body="Hi {{customer_name}}",
        ),
    )

    response = notifications_web.notification_template_preview(
        request=_request(),
        template_id=template.id,
        test_variables_json='{"customer_name":"Jane","invoice_number":"INV-9"}',
        db=db_session,
    )

    assert response.context["rendered_subject"] == "Invoice INV-9"
    assert response.context["rendered_body"] == "Hi Jane"
