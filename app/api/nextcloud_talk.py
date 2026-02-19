from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.nextcloud_talk import (
    NextcloudTalkMessageRequest,
    NextcloudTalkRoomCreateRequest,
    NextcloudTalkRoomListRequest,
)
from app.services.nextcloud_talk import (
    NextcloudTalkClient,
    NextcloudTalkError,
    resolve_talk_client,
)

router = APIRouter(prefix="/nextcloud-talk", tags=["nextcloud-talk"])


def _resolve_client(db: Session, payload) -> NextcloudTalkClient:
    return resolve_talk_client(
        db,
        base_url=payload.base_url,
        username=payload.username,
        app_password=payload.app_password,
        timeout_sec=payload.timeout_sec,
        connector_config_id=payload.connector_config_id,
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
