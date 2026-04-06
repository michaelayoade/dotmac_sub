from __future__ import annotations

from starlette.requests import Request

from app.services import email as email_service
from app.web.admin import system as system_web


def test_email_page_prefills_smtp_sender_form_for_edit(db_session):
    email_service.upsert_smtp_sender(
        db_session,
        sender_key="billing",
        host="smtp.billing.local",
        port=2525,
        username="mailer",
        password="secret",
        from_email="billing@example.com",
        from_name="Billing Sender",
        use_tls=False,
        use_ssl=True,
        is_active=True,
    )
    email_service.set_default_smtp_sender_key(db_session, "billing")

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/system/email",
            "query_string": b"edit_sender=billing",
            "headers": [],
        }
    )

    response = system_web.email_page(request=request, db=db_session)
    form = response.context["smtp_sender_form"]

    assert form["is_editing"] is True
    assert form["sender_key"] == "billing"
    assert form["host"] == "smtp.billing.local"
    assert form["port"] == 2525
    assert form["username"] == "mailer"
    assert form["from_email"] == "billing@example.com"
    assert form["from_name"] == "Billing Sender"
    assert form["use_tls"] is False
    assert form["use_ssl"] is True
    assert form["is_default"] is True

