from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse

from app.db import get_db
from app.schemas.connector import (
    ConnectorConfigCreate,
    ConnectorConfigRead,
    ConnectorConfigUpdate,
)
from app.services import connector as connector_service

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.post(
    "/configs",
    response_model=ConnectorConfigRead,
    status_code=status.HTTP_201_CREATED,
)
def create_connector_config(
    payload: ConnectorConfigCreate, db: Session = Depends(get_db)
):
    return connector_service.connector_configs.create(db, payload)


@router.get("/configs/{config_id}", response_model=ConnectorConfigRead)
def get_connector_config(config_id: str, db: Session = Depends(get_db)):
    return connector_service.connector_configs.get(db, config_id)


@router.get("/configs", response_model=ListResponse[ConnectorConfigRead])
def list_connector_configs(
    connector_type: str | None = None,
    auth_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return connector_service.connector_configs.list_response(
        db, connector_type, auth_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/configs/{config_id}", response_model=ConnectorConfigRead)
def update_connector_config(
    config_id: str, payload: ConnectorConfigUpdate, db: Session = Depends(get_db)
):
    return connector_service.connector_configs.update(db, config_id, payload)


@router.delete("/configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector_config(config_id: str, db: Session = Depends(get_db)):
    connector_service.connector_configs.delete(db, config_id)