"""Fast unit evidence for the protected customer attachment adapter and owner read."""

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.api import customer_support_attachments as api
from app.db import get_db
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment
from app.services import web_support_tickets as owner
from app.services.auth_dependencies import require_user_auth
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult


@dataclass(kw_only=True)
class AttachmentRig:
    db: Session
    subscriber_id: UUID
    ticket: Ticket
    record: StoredFile
    comment: TicketComment
    principal: dict[str, object]
    client: TestClient
    streamed: list[UUID]

    @property
    def path(self) -> str:
        return (
            f"/api/v1/me/support/tickets/{self.ticket.id}/attachments/{self.record.id}"
        )


@pytest.fixture
def attachment_rig(
    db_session: Session, subscriber: Subscriber, monkeypatch: pytest.MonkeyPatch
) -> Iterator[AttachmentRig]:
    subscriber_id = subscriber.id
    ticket = Ticket(title="Customer attachment", subscriber_id=subscriber_id)
    comment = TicketComment(ticket=ticket, body="Visible evidence", is_internal=False)
    db_session.add_all([ticket, comment])
    db_session.flush()
    record = StoredFile(
        entity_type="support_ticket_comment_attachment",
        entity_id=str(ticket.id),
        original_filename="evidence.jpg",
        storage_key_or_relative_path=f"private/{ticket.id}/evidence.jpg",
        file_size=4,
        content_type="image/jpeg",
        storage_provider="s3",
    )
    db_session.add(record)
    db_session.flush()
    comment.attachments = [{"stored_file_id": str(record.id)}]
    db_session.commit()
    principal: dict[str, object] = {
        "principal_type": "subscriber",
        "subscriber_id": str(subscriber_id),
    }
    streamed: list[UUID] = []

    def stream(file: StoredFile) -> StreamResult:
        streamed.append(file.id)
        return StreamResult(
            chunks=iter([b"data"]), content_type="image/jpeg", content_length=4
        )

    def authenticated() -> dict[str, object]:
        return principal

    def database() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(file_uploads, "stream_file", stream)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/me")
    app.dependency_overrides[require_user_auth] = authenticated
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield AttachmentRig(
            db=db_session,
            subscriber_id=subscriber_id,
            ticket=ticket,
            record=record,
            comment=comment,
            principal=principal,
            client=client,
            streamed=streamed,
        )


def test_owned_visible_attachment_streams_private_content(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    response = rig.client.get(rig.path)
    assert response.status_code == 200 and response.content == b"data"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-length"] == "4"
    assert "private/" not in str(response.headers)
    assert rig.streamed == [rig.record.id]


@pytest.mark.parametrize(
    "principal_type", ["system_user", "vendor", "api_key", "reseller_user"]
)
def test_noncustomer_principals_cannot_read(
    attachment_rig: AttachmentRig, principal_type: str
) -> None:
    rig = attachment_rig
    rig.principal["principal_type"] = principal_type
    assert rig.client.get(rig.path).status_code == 403
    assert not rig.streamed


@pytest.mark.parametrize(
    "refusal",
    [
        "wrong_customer",
        "internal_comment",
        "deleted_file",
        "wrong_entity",
        "unreferenced",
        "missing_ticket",
        "missing_file",
    ],
)
def test_missing_or_hidden_attachments_have_identical_safe_response(
    attachment_rig: AttachmentRig, refusal: str
) -> None:
    rig = attachment_rig
    path = rig.path
    if refusal == "wrong_customer":
        rig.principal["subscriber_id"] = str(uuid4())
    elif refusal == "internal_comment":
        rig.comment.is_internal = True
    elif refusal == "deleted_file":
        rig.record.is_deleted = True
    elif refusal == "wrong_entity":
        rig.record.entity_type = "project_attachment"
    elif refusal == "unreferenced":
        rig.comment.attachments = []
    elif refusal == "missing_ticket":
        path = f"/api/v1/me/support/tickets/{uuid4()}/attachments/{rig.record.id}"
    else:
        path = f"/api/v1/me/support/tickets/{rig.ticket.id}/attachments/{uuid4()}"
    rig.db.commit()
    response = rig.client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Attachment not found"}
    assert response.headers["cache-control"] == "private, no-store"
    assert not rig.streamed


def test_internal_ticket_description_attachment_is_hidden(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    rig.comment.attachments = []
    rig.ticket.description_is_internal = True
    rig.ticket.attachments = [{"stored_file_id": str(rig.record.id)}]
    rig.db.commit()
    assert rig.client.get(rig.path).status_code == 404
    assert not rig.streamed


def test_missing_physical_bytes_have_safe_notfound(
    attachment_rig: AttachmentRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(file: StoredFile) -> StreamResult:
        raise ObjectNotFoundError("private storage detail")

    monkeypatch.setattr(file_uploads, "stream_file", missing)
    response = attachment_rig.client.get(attachment_rig.path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Attachment not found"}


def test_owner_refreshes_cached_ownership_before_streaming(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    rig.db.execute(
        update(Ticket).where(Ticket.id == rig.ticket.id).values(subscriber_id=None),
        execution_options={"synchronize_session": False},
    )
    with pytest.raises(owner.CustomerTicketAttachmentNotFound):
        owner.customer_ticket_attachment_content(
            rig.db,
            query=owner.CustomerTicketAttachmentQuery(
                subscriber_id=rig.subscriber_id,
                ticket_id=rig.ticket.id,
                file_id=rig.record.id,
            ),
        )
    assert not rig.streamed


def test_owner_refreshes_cached_visibility_before_streaming(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    rig.db.execute(
        update(TicketComment)
        .where(TicketComment.id == rig.comment.id)
        .values(is_internal=True),
        execution_options={"synchronize_session": False},
    )
    with pytest.raises(owner.CustomerTicketAttachmentNotFound):
        owner.customer_ticket_attachment_content(
            rig.db,
            query=owner.CustomerTicketAttachmentQuery(
                subscriber_id=rig.subscriber_id,
                ticket_id=rig.ticket.id,
                file_id=rig.record.id,
            ),
        )
    assert not rig.streamed


def test_owner_closes_provider_and_rechecks_visibility_after_io(
    attachment_rig: AttachmentRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = attachment_rig
    closed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        try:
            rig.db.execute(
                update(TicketComment)
                .where(TicketComment.id == rig.comment.id)
                .values(is_internal=True),
                execution_options={"synchronize_session": False},
            )
            yield b"data"
        finally:
            closed.append(True)

    def stream(file: StoredFile) -> StreamResult:
        return StreamResult(
            chunks=chunks(), content_type="image/jpeg", content_length=4
        )

    monkeypatch.setattr(file_uploads, "stream_file", stream)
    response = rig.client.get(rig.path)
    assert response.status_code == 404 and response.content != b"data"
    assert closed == [True]


def test_owner_enforces_actual_byte_bound_and_closes_early(
    attachment_rig: AttachmentRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        try:
            yield b"x" * (owner.MAX_ATTACHMENT_BYTES + 1)
        finally:
            closed.append(True)

    def stream(file: StoredFile) -> StreamResult:
        return StreamResult(
            chunks=chunks(), content_type="image/jpeg", content_length=None
        )

    monkeypatch.setattr(file_uploads, "stream_file", stream)
    assert attachment_rig.client.get(attachment_rig.path).status_code == 404
    assert closed == [True]


def test_owned_public_description_attachment_is_readable(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    rig.comment.attachments = []
    rig.ticket.description_is_internal = False
    rig.ticket.attachments = [{"stored_file_id": str(rig.record.id)}]
    rig.db.commit()
    assert rig.client.get(rig.path).content == b"data"


@pytest.mark.parametrize("deleted_row", ["file", "ticket"])
def test_owner_returns_safe_notfound_for_hard_deleted_row_after_io(
    attachment_rig: AttachmentRig, monkeypatch: pytest.MonkeyPatch, deleted_row: str
) -> None:
    rig = attachment_rig
    ticket_id, file_id = rig.ticket.id, rig.record.id
    path = rig.path
    closed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        try:
            if deleted_row == "file":
                rig.db.execute(
                    delete(StoredFile).where(StoredFile.id == file_id),
                    execution_options={"synchronize_session": False},
                )
            else:
                rig.db.execute(
                    delete(TicketComment).where(TicketComment.ticket_id == ticket_id),
                    execution_options={"synchronize_session": False},
                )
                rig.db.execute(
                    delete(Ticket).where(Ticket.id == ticket_id),
                    execution_options={"synchronize_session": False},
                )
            yield b"data"
        finally:
            closed.append(True)

    def stream(file: StoredFile) -> StreamResult:
        return StreamResult(
            chunks=chunks(), content_type="image/jpeg", content_length=4
        )

    monkeypatch.setattr(file_uploads, "stream_file", stream)
    response = rig.client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Attachment not found"}
    assert response.headers["cache-control"] == "private, no-store"
    assert closed == [True]


def test_owner_refuses_cached_hard_deleted_file_before_provider_io(
    attachment_rig: AttachmentRig,
) -> None:
    rig = attachment_rig
    ticket_id, file_id = rig.ticket.id, rig.record.id
    rig.db.execute(
        delete(StoredFile).where(StoredFile.id == file_id),
        execution_options={"synchronize_session": False},
    )
    with pytest.raises(owner.CustomerTicketAttachmentNotFound):
        owner.customer_ticket_attachment_content(
            rig.db,
            query=owner.CustomerTicketAttachmentQuery(
                subscriber_id=rig.subscriber_id, ticket_id=ticket_id, file_id=file_id
            ),
        )
    assert not rig.streamed
