"""GenieACS inform webhook receiver.

Receives callbacks from GenieACS when CPE devices send Inform messages.
Updates last_inform_at and optionally creates session records.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import tr069 as tr069_service

router = APIRouter(prefix="/tr069", tags=["tr069-inform"])


class InformPayload(BaseModel):
    """GenieACS inform callback payload."""

    serial_number: str | None = None
    oui: str | None = None
    product_class: str | None = None
    event: str = Field(default="periodic")
    device_id: str | None = None


@router.post("/inform")
def receive_inform(
    payload: InformPayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Receive GenieACS inform webhook callback.

    GenieACS can be configured to POST to this endpoint on device inform.
    The payload contains device identity and event information.
    """
    return tr069_service.receive_inform(
        db,
        serial_number=payload.serial_number,
        device_id_raw=payload.device_id,
        event=payload.event,
    )
