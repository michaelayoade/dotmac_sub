from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.services.response import list_response
from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.external import (
    ExternalReferenceCreate,
    ExternalReferenceRead,
    ExternalReferenceSync,
    ExternalReferenceUpdate,
)
from app.services import external as external_service

router = APIRouter()


@router.post(
    "/external-references",
    response_model=ExternalReferenceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["external-references"],
)
def create_external_reference(
    payload: ExternalReferenceCreate, db: Session = Depends(get_db)
):
    return external_service.external_references.create(db, payload)


@router.get(
    "/external-references/{ref_id}",
    response_model=ExternalReferenceRead,
    tags=["external-references"],
)
def get_external_reference(ref_id: str, db: Session = Depends(get_db)):
    return external_service.external_references.get(db, ref_id)


@router.get(
    "/external-references",
    response_model=ListResponse[ExternalReferenceRead],
    tags=["external-references"],
)
def list_external_references(
    connector_config_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    external_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return external_service.external_references.list_response(
        db,
        connector_config_id,
        entity_type,
        entity_id,
        external_id,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/external-references/{ref_id}",
    response_model=ExternalReferenceRead,
    tags=["external-references"],
)
def update_external_reference(
    ref_id: str, payload: ExternalReferenceUpdate, db: Session = Depends(get_db)
):
    return external_service.external_references.update(db, ref_id, payload)


@router.delete(
    "/external-references/{ref_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["external-references"],
)
def delete_external_reference(ref_id: str, db: Session = Depends(get_db)):
    external_service.external_references.delete(db, ref_id)


@router.post(
    "/external-references/sync",
    response_model=ExternalReferenceRead,
    status_code=status.HTTP_200_OK,
    tags=["external-references"],
)
def sync_external_reference(
    payload: ExternalReferenceSync, db: Session = Depends(get_db)
):
    return external_service.sync_reference(db, payload)