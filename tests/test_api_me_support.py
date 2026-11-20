"""Unit tests for the self-scoped customer support endpoints in app/api/me.py.

These cover the security-critical behaviours of the customer-facing support
surface: non-subscriber principals are rejected (403), every ticket is forced to
the caller's own subscriber_id, a customer can only reach their own tickets
(no IDOR), and staff-internal notes are never exposed or createable.
"""

import types
import uuid

import pytest
from fastapi import HTTPException

from app.api import me as me_api
from app.models.support import TicketChannel, TicketCommentAuthorType
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services.file_storage import FileValidationError


def _subscriber_principal():
    return {"principal_type": "subscriber", "subscriber_id": str(uuid.uuid4())}


def _system_user_principal():
    return {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}


def test_my_tickets_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_list_response(db, **kwargs):
        captured.update(kwargs)
        return {"items": [], "count": 0, "limit": kwargs["limit"], "offset": 0}

    monkeypatch.setattr(
        me_api.support_service.tickets, "list_response", fake_list_response
    )

    me_api.my_tickets(
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=None,
        principal=principal,
    )
    assert captured["subscriber_id"] == principal["subscriber_id"]


def test_my_tickets_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_tickets(
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_my_ticket_returns_own_ticket(monkeypatch):
    principal = _subscriber_principal()
    ticket = types.SimpleNamespace(subscriber_id=principal["subscriber_id"])
    monkeypatch.setattr(me_api.support_service.tickets, "get", lambda db, tid: ticket)

    assert me_api.my_ticket(ticket_id="t1", db=None, principal=principal) is ticket


def test_my_ticket_404_for_other_subscribers_ticket(monkeypatch):
    principal = _subscriber_principal()
    # Ticket owned by someone else — must 404 (not 403) so ids can't be probed.
    other = types.SimpleNamespace(subscriber_id=str(uuid.uuid4()))
    monkeypatch.setattr(me_api.support_service.tickets, "get", lambda db, tid: other)

    with pytest.raises(HTTPException) as exc:
        me_api.my_ticket(ticket_id="t1", db=None, principal=principal)
    assert exc.value.status_code == 404


def test_my_create_ticket_forces_caller_scope(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_create(db, *, command, request=None):
        captured["payload"] = command.payload
        captured["actor_id"] = command.actor_id
        captured["submission_id"] = command.submission_id
        return types.SimpleNamespace(ticket=command.payload, replayed=False)

    monkeypatch.setattr(
        me_api.support_service.tickets, "create_customer_submission", fake_create
    )
    monkeypatch.setattr(
        me_api.db_session_adapter, "release_read_transaction", lambda db: None
    )

    # The endpoint now accepts multipart/form-data (form fields + a repeatable
    # ``attachments`` file field), not a JSON body.
    me_api.my_create_ticket(
        request=None,
        title="No internet",
        description=None,
        priority="normal",
        ticket_type=None,
        submission_id=None,
        duplicate_override=False,
        attachments=[],
        db=None,
        principal=principal,
    )
    # subscriber_id is forced to the caller; channel is forced to web.
    assert str(captured["payload"].subscriber_id) == principal["subscriber_id"]
    assert captured["payload"].channel == TicketChannel.web
    assert captured["actor_id"] == principal["subscriber_id"]
    assert isinstance(captured["submission_id"], uuid.UUID)


def test_my_create_ticket_maps_invalid_file_to_400(monkeypatch):
    def reject_upload(db, *, command, request=None):
        raise FileValidationError("Invalid file contents")

    monkeypatch.setattr(
        me_api.support_service.tickets, "create_customer_submission", reject_upload
    )
    monkeypatch.setattr(
        me_api.db_session_adapter, "release_read_transaction", lambda db: None
    )
    with pytest.raises(HTTPException) as exc:
        me_api.my_create_ticket(
            request=None,
            title="No internet",
            description=None,
            priority="normal",
            ticket_type=None,
            submission_id=uuid.uuid4(),
            duplicate_override=False,
            attachments=[],
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_attachment"


def test_my_create_ticket_warns_then_accepts_create_anyway(db_session, subscriber):
    existing = support_service.tickets.create(
        db_session,
        TicketCreate(title="No internet", subscriber_id=subscriber.id),
    )
    key = uuid.uuid4()
    kwargs = {
        "request": None,
        "title": existing.title,
        "description": None,
        "priority": "normal",
        "ticket_type": None,
        "submission_id": key,
        "attachments": [],
        "db": db_session,
        "principal": {
            "principal_type": "subscriber",
            "subscriber_id": str(subscriber.id),
        },
    }

    with pytest.raises(HTTPException) as warning:
        me_api.my_create_ticket(**kwargs, duplicate_override=False)
    assert warning.value.status_code == 409
    assert warning.value.detail["code"] == "ticket_duplicate_warning"
    assert warning.value.detail["matches"] == [
        {
            "ticket_id": str(existing.id),
            "reference": existing.number,
            "title": existing.title,
            "status": existing.status,
        }
    ]

    created = me_api.my_create_ticket(**kwargs, duplicate_override=True)
    assert created.id != existing.id
    assert created.customer_submission_id == key
    assert created.metadata_["duplicate_override"] is True


def test_my_ticket_comments_hide_internal_notes(monkeypatch):
    principal = _subscriber_principal()
    ticket = types.SimpleNamespace(subscriber_id=principal["subscriber_id"])
    monkeypatch.setattr(me_api.support_service.tickets, "get", lambda db, tid: ticket)
    monkeypatch.setattr(
        me_api.support_service.ticket_comments,
        "list",
        lambda db, tid, limit=100, offset=0: [
            types.SimpleNamespace(is_internal=False, body="public reply"),
            types.SimpleNamespace(is_internal=True, body="staff-only note"),
        ],
    )

    result = me_api.my_ticket_comments(
        ticket_id="t1", limit=100, offset=0, db=None, principal=principal
    )
    assert result["count"] == 1
    assert [c.body for c in result["items"]] == ["public reply"]


def test_my_add_ticket_comment_forces_public(monkeypatch):
    principal = _subscriber_principal()
    ticket = types.SimpleNamespace(subscriber_id=principal["subscriber_id"])
    captured = {}
    monkeypatch.setattr(me_api.support_service.tickets, "get", lambda db, tid: ticket)

    def fake_create_comment(db, ticket_id, payload, actor_id=None, request=None):
        captured["payload"] = payload
        return payload

    monkeypatch.setattr(
        me_api.support_service.tickets, "create_comment", fake_create_comment
    )
    monkeypatch.setattr(
        me_api.db_session_adapter, "release_read_transaction", lambda db: None
    )

    me_api.my_add_ticket_comment(
        ticket_id="t1",
        request=None,
        body="any update?",
        attachments=[],
        db=None,
        principal=principal,
    )
    # A customer can never post a staff-internal note.
    assert captured["payload"].is_internal is False
    assert captured["payload"].author_type == TicketCommentAuthorType.customer
    assert str(captured["payload"].author_person_id) == principal["subscriber_id"]


def test_my_add_ticket_comment_maps_oversized_attachment_to_413(monkeypatch):
    from app.services import web_support_tickets

    principal = _subscriber_principal()
    ticket_id = str(uuid.uuid4())
    ticket = types.SimpleNamespace(subscriber_id=principal["subscriber_id"])
    monkeypatch.setattr(me_api.support_service.tickets, "get", lambda db, tid: ticket)
    monkeypatch.setattr(
        me_api.db_session_adapter, "release_read_transaction", lambda db: None
    )

    def reject(*_args, **_kwargs):
        raise web_support_tickets.TicketAttachmentValidationError(
            kind=web_support_tickets.TicketAttachmentValidationKind.too_large,
            filename="diagnostic.pdf",
            message="diagnostic.pdf: max file size is 5 MB",
        )

    monkeypatch.setattr(web_support_tickets, "upload_ticket_attachments", reject)

    with pytest.raises(HTTPException) as exc_info:
        me_api.my_add_ticket_comment(
            ticket_id=ticket_id,
            request=None,
            body="See diagnostics",
            attachments=[types.SimpleNamespace(filename="diagnostic.pdf")],
            db=None,
            principal=principal,
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "support.ticket_attachment.too_large"
