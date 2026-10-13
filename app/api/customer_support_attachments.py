"""Authenticated customer content reads for exact visible ticket attachments."""

from collections.abc import Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_support_tickets
from app.services.auth_dependencies import require_user_auth
from app.services.file_storage import build_content_disposition

router = APIRouter(prefix="/support/tickets", tags=["customer-support-attachments"])
_CACHE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


def _subscriber_id(principal: Mapping[str, object]) -> UUID:
    if principal.get("principal_type") != "subscriber":
        raise HTTPException(
            status_code=403,
            detail="A customer account is required",
            headers=_CACHE_HEADERS,
        )
    try:
        return UUID(str(principal["subscriber_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=403,
            detail="A customer account is required",
            headers=_CACHE_HEADERS,
        ) from exc


@router.get("/{ticket_id}/attachments/{file_id}")
def attachment_content(
    ticket_id: UUID,
    file_id: UUID,
    principal: Mapping[str, object] = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> Response:
    try:
        content = web_support_tickets.customer_ticket_attachment_content(
            db,
            query=web_support_tickets.CustomerTicketAttachmentQuery(
                subscriber_id=_subscriber_id(principal),
                ticket_id=ticket_id,
                file_id=file_id,
            ),
        )
    except web_support_tickets.CustomerTicketAttachmentNotFound as exc:
        raise HTTPException(
            status_code=404, detail="Attachment not found", headers=_CACHE_HEADERS
        ) from exc
    headers = {
        **_CACHE_HEADERS,
        "Content-Disposition": build_content_disposition(content.filename),
    }
    return Response(content.data, media_type=content.content_type, headers=headers)
