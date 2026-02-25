"""Authenticated private file download endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.stored_file import StoredFile
from app.services.file_storage import build_content_disposition, file_uploads
from app.services.object_storage import ObjectNotFoundError

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{file_id}/download")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    file_record = db.get(StoredFile, file_uuid)
    if not file_record or file_record.is_deleted:
        raise HTTPException(status_code=404, detail="File not found")

    request_org = file_uploads.resolve_user_organization(
        db, current_user["subscriber_id"]
    )
    file_uploads.assert_tenant_access(file_record, request_org)

    try:
        stream = file_uploads.stream_file(file_record)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    headers = {"Content-Disposition": build_content_disposition(file_record.original_filename)}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )
