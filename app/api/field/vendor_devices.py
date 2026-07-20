from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import DeviceTokenRead, DeviceTokenRegister
from app.services.field.vendor_auth import require_field_vendor_token
from app.services.field.vendor_devices import register_vendor_device

router = APIRouter(tags=["field-vendor-devices"])


@router.post(
    "/vendor/devices",
    response_model=DeviceTokenRead,
    status_code=status.HTTP_201_CREATED,
)
def register_vendor_field_device(
    payload: DeviceTokenRegister,
    vendor: dict = Depends(require_field_vendor_token),
    db: Session = Depends(get_db),
):
    return register_vendor_device(
        db,
        vendor_user_id=vendor["vendor_user_id"],
        token=payload.fcm_token,
        platform=payload.platform,
        app_version=payload.app_version,
    )
