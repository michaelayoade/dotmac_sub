from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import DeviceTokenRead, DeviceTokenRegister
from app.services import push as push_service
from app.services.auth_dependencies import require_user_auth

router = APIRouter(tags=["field-devices"])


def _system_user_id(auth: dict) -> str:
    if auth.get("principal_type") != "system_user":
        raise HTTPException(
            status_code=403, detail="Field device registration requires a staff account"
        )
    return str(auth.get("principal_id") or auth.get("person_id") or "")


@router.post(
    "/devices",
    response_model=DeviceTokenRead,
    status_code=status.HTTP_201_CREATED,
)
def register_device(
    payload: DeviceTokenRegister,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return push_service.register_system_user_token(
        db,
        _system_user_id(auth),
        payload.fcm_token,
        platform=payload.platform,
        app_version=payload.app_version,
    )


@router.get("/devices", response_model=ListResponse[DeviceTokenRead])
def list_devices(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = push_service.list_system_user_devices(db, _system_user_id(auth))
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def deregister_device(
    device_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    push_service.unregister_system_user_token(db, _system_user_id(auth), device_id)
    return None
