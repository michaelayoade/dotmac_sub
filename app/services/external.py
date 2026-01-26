from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorConfig
from app.models.external import ExternalEntityType, ExternalReference
from app.schemas.external import (
    ExternalReferenceCreate,
    ExternalReferenceSync,
    ExternalReferenceUpdate,
)
from app.services.common import validate_enum, apply_pagination, apply_ordering, coerce_uuid
from app.services.response import ListResponseMixin


class ExternalReferences(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ExternalReferenceCreate):
        connector = db.get(ConnectorConfig, coerce_uuid(payload.connector_config_id))
        if not connector:
            raise HTTPException(status_code=404, detail="Connector config not found")
        ref = ExternalReference(**payload.model_dump())
        db.add(ref)
        db.commit()
        db.refresh(ref)
        return ref

    @staticmethod
    def get(db: Session, ref_id: str):
        ref = db.get(ExternalReference, coerce_uuid(ref_id))
        if not ref:
            raise HTTPException(status_code=404, detail="External reference not found")
        return ref

    @staticmethod
    def list(
        db: Session,
        connector_config_id: str | None,
        entity_type: str | None,
        entity_id: str | None,
        external_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ExternalReference)
        if connector_config_id:
            query = query.filter(
                ExternalReference.connector_config_id == connector_config_id
            )
        if entity_type:
            query = query.filter(
                ExternalReference.entity_type
                == validate_enum(entity_type, ExternalEntityType, "entity_type")
            )
        if entity_id:
            query = query.filter(ExternalReference.entity_id == entity_id)
        if external_id:
            query = query.filter(ExternalReference.external_id == external_id)
        if is_active is None:
            query = query.filter(ExternalReference.is_active.is_(True))
        else:
            query = query.filter(ExternalReference.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ExternalReference.created_at, "external_id": ExternalReference.external_id},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, ref_id: str, payload: ExternalReferenceUpdate):
        ref = db.get(ExternalReference, coerce_uuid(ref_id))
        if not ref:
            raise HTTPException(status_code=404, detail="External reference not found")
        data = payload.model_dump(exclude_unset=True)
        if "connector_config_id" in data and data["connector_config_id"]:
            connector = db.get(ConnectorConfig, coerce_uuid(data["connector_config_id"]))
            if not connector:
                raise HTTPException(status_code=404, detail="Connector config not found")
        for key, value in data.items():
            setattr(ref, key, value)
        db.commit()
        db.refresh(ref)
        return ref

    @staticmethod
    def delete(db: Session, ref_id: str):
        ref = db.get(ExternalReference, coerce_uuid(ref_id))
        if not ref:
            raise HTTPException(status_code=404, detail="External reference not found")
        ref.is_active = False
        db.commit()


external_references = ExternalReferences()


def _get_reference_for_sync(db: Session, payload: ExternalReferenceSync) -> ExternalReference | None:
    return (
        db.query(ExternalReference)
        .filter(ExternalReference.connector_config_id == payload.connector_config_id)
        .filter(ExternalReference.entity_type == payload.entity_type)
        .filter(
            (ExternalReference.entity_id == payload.entity_id)
            | (ExternalReference.external_id == payload.external_id)
        )
        .first()
    )


def sync_reference(db: Session, payload: ExternalReferenceSync) -> ExternalReference:
    connector = db.get(ConnectorConfig, coerce_uuid(payload.connector_config_id))
    if not connector:
        raise HTTPException(status_code=404, detail="Connector config not found")
    ref = _get_reference_for_sync(db, payload)
    data = payload.model_dump(exclude_unset=True)
    if ref:
        for key, value in data.items():
            setattr(ref, key, value)
        ref.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ref)
        return ref
    ref = ExternalReference(**data)
    ref.last_synced_at = datetime.now(timezone.utc)
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref
