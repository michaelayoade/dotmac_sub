"""GenieACS inform webhook receiver.

Receives callbacks from GenieACS when CPE devices send Inform messages.
Updates last_inform_at and optionally creates session records.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.acs_client import create_acs_event_ingestor

router = APIRouter(prefix="/tr069", tags=["tr069-inform"])


class InformPayload(BaseModel):
    """GenieACS inform callback payload."""

    model_config = ConfigDict(extra="allow")

    serial_number: str | None = None
    oui: str | None = None
    product_class: str | None = None
    event: Any = Field(default="periodic")
    device_id: str | None = None
    request_id: str | None = None
    acs_server_id: str | None = None


@router.post("/inform")
def receive_inform(
    request: Request,
    payload: InformPayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Receive GenieACS inform webhook callback.

    GenieACS can be configured to POST to this endpoint on device inform.
    The payload contains device identity and event information.
    """
    ingestor = create_acs_event_ingestor()
    return ingestor.receive_inform(
        db,
        serial_number=payload.serial_number,
        device_id_raw=payload.device_id,
        event=payload.event,
        raw_payload=payload.model_dump(mode="json"),
        request_id=payload.request_id
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id"),
        remote_addr=request.client.host if request.client else None,
        headers={
            "user-agent": request.headers.get("user-agent"),
            "x-forwarded-for": request.headers.get("x-forwarded-for"),
            "x-real-ip": request.headers.get("x-real-ip"),
        },
        oui=payload.oui,
        product_class=payload.product_class,
        acs_server_id=payload.acs_server_id,
    )
