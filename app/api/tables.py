from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.schemas.table_config import (
    TableColumnPreference,
    TableColumnsResponse,
    TableDataResponse,
)
from app.services.table_config import TableConfigurationService, TableRegistry

router = APIRouter(prefix="/tables", tags=["tables"])


@router.get("/{table_key}/columns", response_model=TableColumnsResponse)
def get_table_columns(
    table_key: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    if not TableRegistry.exists(table_key):
        raise HTTPException(status_code=404, detail="Unregistered tableKey")

    user_id = UUID(auth["subscriber_id"])
    columns = TableConfigurationService.get_columns(db, user_id, table_key)
    available_columns = TableConfigurationService.get_available_columns(table_key)

    return TableColumnsResponse(
        table_key=table_key,
        available_columns=available_columns,
        columns=columns,
    )


@router.post("/{table_key}/columns", response_model=TableColumnsResponse)
def save_table_columns(
    table_key: str,
    payload: list[TableColumnPreference],
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    if not TableRegistry.exists(table_key):
        raise HTTPException(status_code=404, detail="Unregistered tableKey")

    user_id = UUID(auth["subscriber_id"])
    columns = TableConfigurationService.save_columns(db, user_id, table_key, payload)

    return TableColumnsResponse(
        table_key=table_key,
        available_columns=TableConfigurationService.get_available_columns(table_key),
        columns=columns,
    )


@router.get("/{table_key}/data", response_model=TableDataResponse)
def get_table_data(
    table_key: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    if not TableRegistry.exists(table_key):
        raise HTTPException(status_code=404, detail="Unregistered tableKey")

    user_id = UUID(auth["subscriber_id"])
    request_params = dict(request.query_params)

    columns, items, count = TableConfigurationService.apply_query_config(
        db,
        user_id,
        table_key,
        request_params,
    )

    limit = int(request_params.get("limit", 50) or 50)
    offset = int(request_params.get("offset", 0) or 0)

    return TableDataResponse(
        table_key=table_key,
        columns=columns,
        items=items,
        count=count,
        limit=limit,
        offset=offset,
    )
