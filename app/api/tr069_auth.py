"""Lightweight GenieACS authentication webhook."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import tr069_auth

router = APIRouter(prefix="/tr069", tags=["tr069-auth"])


@router.get("/auth")
def get_device_credentials(
    serial_number: str = Query(..., description="Device serial number"),
    type: Literal["connection_request", "cpe_auth"] = Query(
        ..., description="Credential type: connection_request or cpe_auth"
    ),
    db: Session = Depends(get_db),
) -> dict[str, str | None]:
    """Return per-device credentials for GenieACS auth extensions."""
    return tr069_auth.get_device_credentials(db, serial_number, type)
