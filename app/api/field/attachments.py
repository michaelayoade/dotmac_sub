from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.execution import (
    field_command_context,
    field_domain_errors,
    field_system_user_id,
)
from app.api.field.work_order_compat import resolve_work_order_id
from app.schemas.common import ListResponse
from app.schemas.field import FieldAttachmentRead
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.attachments import (
    field_attachments,
    parse_captured_at,
)
from app.services.field.execution_contracts import (
    CreateFieldAttachment,
    DeleteFieldAttachment,
    FieldAttachmentIdentity,
    FieldAttachmentQuery,
)
from app.services.file_storage import build_content_disposition

router = APIRouter(tags=["field-attachments"])


@router.post(
    "/attachments",
    response_model=FieldAttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_field_attachment(
    file: UploadFile = File(...),
    kind: str = Form(default="photo"),
    client_ref: UUID | None = Form(default=None),
    work_order_id: str | None = Form(default=None),
    crm_work_order_id: str | None = Form(default=None, deprecated=True),
    note_id: UUID | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    captured_at: str | None = Form(default=None),
    signer_name: str | None = Form(default=None),
    asset_type: str | None = Form(default=None),
    asset_id: UUID | None = Form(default=None),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    principal_id = field_system_user_id(auth)
    with field_domain_errors():
        command = CreateFieldAttachment(
            context=field_command_context(
                principal_id, reason="field_attachment_creation", request_id=client_ref
            ),
            requester_system_user_id=principal_id,
            kind=kind,
            file_name=file.filename or "upload",
            mime_type=file.content_type,
            content=file.file.read(),
            client_ref=client_ref,
            public_id=resolved_work_order_id,
            note_id=note_id,
            latitude=latitude,
            longitude=longitude,
            captured_at=parse_captured_at(captured_at),
            signer_name=signer_name,
            asset_type=asset_type,
            asset_id=asset_id,
        )
        db_session_adapter.release_read_transaction(db)
        return field_attachments.create(db=db, command=command)


@router.get("/attachments", response_model=ListResponse[FieldAttachmentRead])
def list_field_attachments(
    work_order_id: str | None = None,
    crm_work_order_id: str | None = Query(default=None, deprecated=True),
    note_id: UUID | None = None,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    with field_domain_errors():
        items = field_attachments.list(
            db=db,
            query=FieldAttachmentQuery(
                requester_system_user_id=field_system_user_id(auth),
                public_id=resolved_work_order_id,
                note_id=note_id,
                kind=kind,
                limit=limit,
                offset=offset,
            ),
        )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/attachments/{attachment_id}", response_model=FieldAttachmentRead)
def get_field_attachment(
    attachment_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    with field_domain_errors():
        return field_attachments.get(
            db=db,
            query=FieldAttachmentIdentity(
                requester_system_user_id=field_system_user_id(auth),
                attachment_id=attachment_id,
            ),
        )


@router.get("/attachments/{attachment_id}/content")
def download_field_attachment(
    attachment_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    with field_domain_errors():
        attachment, stream = field_attachments.get_content(
            db=db,
            query=FieldAttachmentIdentity(
                requester_system_user_id=field_system_user_id(auth),
                attachment_id=attachment_id,
            ),
        )
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or attachment.mime_type,
        headers={
            "Content-Disposition": build_content_disposition(attachment.file_name)
        },
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_field_attachment(
    attachment_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    principal_id = field_system_user_id(auth)
    with field_domain_errors():
        db_session_adapter.release_read_transaction(db)
        field_attachments.delete(
            db=db,
            command=DeleteFieldAttachment(
                context=field_command_context(
                    principal_id, reason="field_attachment_deletion"
                ),
                requester_system_user_id=principal_id,
                attachment_id=attachment_id,
            ),
        )
