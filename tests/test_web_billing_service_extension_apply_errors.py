"""Apply failures render actionable feedback and submit typed audit evidence."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse

from app.services import service_extensions
from app.services.domain_errors import DomainError
from app.web.admin import billing_extensions


def _request(extension_id: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": f"/admin/billing/service-extensions/{extension_id}/apply",
            "query_string": b"",
            "headers": [],
        }
    )
    request.state.request_id = str(uuid4())
    request.state.user = SimpleNamespace(
        id=uuid4(), first_name="Amina", last_name="Okafor"
    )
    request.state.auth = {"principal_type": "system_user"}
    return request


@pytest.mark.parametrize(
    ("key", "reason", "source_code", "present"),
    [
        (None, "missing_form_key", "missing_form_key", False),
        ("", "invalid_application_key", "empty_idempotency_key", True),
        ("not-a-uuid", "invalid_application_key", "invalid_idempotency_key", True),
    ],
)
def test_apply_form_key_failures_render_specific_message_and_audit(
    monkeypatch, key, reason, source_code, present
):
    extension_id = str(uuid4())
    request = _request(extension_id)
    audited = []
    monkeypatch.setattr(
        service_extensions,
        "record_apply_rejection",
        lambda db, command: audited.append(command),
    )
    monkeypatch.setattr(billing_extensions, "_context", lambda _r, _d, extra: extra)
    monkeypatch.setattr(
        billing_extensions.templates,
        "TemplateResponse",
        lambda _name, context, status_code: HTMLResponse(
            context["failure"].message, status_code=status_code
        ),
    )

    response = billing_extensions.service_extension_apply(
        request, extension_id, idempotency_key=key, db=object()
    )

    assert response.status_code == 422
    assert audited[0].reason.value == reason
    assert audited[0].source_code == source_code
    assert audited[0].key_present is present
    assert audited[0].request_id == request.state.request_id
    assert b"Refresh" in response.body or b"Reload" in response.body


def test_apply_domain_failure_keeps_specific_message_and_audit(monkeypatch):
    extension_id = str(uuid4())
    request = _request(extension_id)
    audited = []
    monkeypatch.setattr(
        service_extensions,
        "record_apply_rejection",
        lambda db, command: audited.append(command),
    )
    monkeypatch.setattr(
        service_extensions,
        "apply_service_extension",
        lambda db, command: (_ for _ in ()).throw(
            DomainError(
                code="financial.service_extensions.self_approval_forbidden",
                message="Different staff member required",
                details={"created_by": "maker-id"},
            )
        ),
    )
    monkeypatch.setattr(billing_extensions, "_context", lambda _r, _d, extra: extra)
    monkeypatch.setattr(
        billing_extensions.templates,
        "TemplateResponse",
        lambda _name, context, status_code: HTMLResponse(
            context["failure"].message, status_code=status_code
        ),
    )

    response = billing_extensions.service_extension_apply(
        request, extension_id, idempotency_key=str(uuid4()), db=object()
    )

    assert response.status_code == 409
    assert b"different authorized staff member" in response.body
    assert (
        audited[0].reason
        is service_extensions.ApplyRejectionReason.self_approval_forbidden
    )
    assert audited[0].creator_id == "maker-id"
