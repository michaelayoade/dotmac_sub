"""Lightweight GenieACS authentication webhook."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.services import tr069_auth

router = APIRouter(prefix="/tr069", tags=["tr069-auth"])


@router.get("/auth")
def get_device_credentials(
    serial_number: str = Query(..., description="Device serial number"),
    type: Literal["connection_request", "cpe_auth"] = Query(
        ..., description="Credential type: connection_request or cpe_auth"
    ),
    shared_secret: str | None = Header(default=None, alias="X-DotMac-TR069-Auth"),
    db: Session = Depends(get_db),
) -> dict[str, str | bool | None]:
    """Return per-device credentials for GenieACS auth extensions."""
    expected_secret = settings.tr069_auth_shared_secret
    if expected_secret and shared_secret != expected_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    if not expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TR-069 auth shared secret is not configured",
        )
    return tr069_auth.get_device_credentials(db, serial_number, type)
