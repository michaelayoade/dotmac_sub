from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.connector import ConnectorConfig
from app.schemas.nextcloud_talk import (
    NextcloudTalkMessageRequest,
    NextcloudTalkRoomCreateRequest,
    NextcloudTalkRoomListRequest,
)
from app.services.common import coerce_uuid
from app.services.nextcloud_talk import NextcloudTalkClient, NextcloudTalkError

router = APIRouter(prefix="/nextcloud-talk", tags=["nextcloud-talk"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_client(db: Session, payload) -> NextcloudTalkClient:
    base_url = payload.base_url
    username = payload.username
    app_password = payload.app_password
    timeout = payload.timeout_sec

    if payload.connector_config_id:
        config = db.get(ConnectorConfig, coerce_uuid(payload.connector_config_id))
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        auth_config = dict(config.auth_config or {})
        base_url = base_url or config.base_url
        username = username or auth_config.get("username")
        app_password = (
            app_password
            or auth_config.get("app_password")
            or auth_config.get("password")
        )
        timeout = timeout or config.timeout_sec or auth_config.get("timeout_sec")

    if not base_url or not username or not app_password:
        raise HTTPException(
            status_code=400,
            detail="Nextcloud Talk credentials are incomplete.",
        )

    return NextcloudTalkClient(
        base_url=base_url,
        username=username,
        app_password=app_password,
        timeout=float(timeout or 30.0),
    )


@router.post("/rooms/list", response_model=list[dict])
def list_rooms(payload: NextcloudTalkRoomListRequest, db: Session = Depends(get_db)):
    client = _resolve_client(db, payload)
    try:
        return client.list_rooms()
    except NextcloudTalkError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post("/rooms", response_model=dict)
def create_room(payload: NextcloudTalkRoomCreateRequest, db: Session = Depends(get_db)):
    client = _resolve_client(db, payload)
    try:
        return client.create_room(
            room_name=payload.room_name,
            room_type=payload.room_type,
            options=payload.options,
        )
    except NextcloudTalkError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post("/rooms/{room_token}/messages", response_model=dict)
def post_message(
    room_token: str, payload: NextcloudTalkMessageRequest, db: Session = Depends(get_db)
):
    client = _resolve_client(db, payload)
    try:
        return client.post_message(
            room_token=room_token,
            message=payload.message,
            options=payload.options,
        )
    except NextcloudTalkError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
