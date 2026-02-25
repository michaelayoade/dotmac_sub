"""Public branding asset routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.stored_file import StoredFile
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError

router = APIRouter(prefix="/branding", tags=["public-branding"])


@router.get("/assets/{file_id}")
def branding_asset(file_id: str, db: Session = Depends(get_db)):
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        return Response(status_code=404)

    record = db.get(StoredFile, file_uuid)
    if not record or record.is_deleted or record.entity_type != "branding_asset":
        return Response(status_code=404)

    try:
        stream = file_uploads.stream_file(record)
    except ObjectNotFoundError:
        return Response(status_code=404)

    headers: dict[str, str] = {"Cache-Control": "public, max-age=3600"}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )
