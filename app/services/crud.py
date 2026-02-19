"""Generic CRUD manager utilities for service-layer boilerplate reduction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar, cast

from fastapi import HTTPException

from app.services.response import ListResponseMixin

TModel = TypeVar("TModel")


class CRUDManager(ListResponseMixin, Generic[TModel]):
    """Reusable CRUD primitives for services with model-only persistence logic."""

    model: type[TModel] | None = None
    not_found_detail: str = "Resource not found"
    soft_delete_field: str | None = None
    soft_delete_value = False

    @classmethod
    def _require_model(cls) -> type[TModel]:
        if cls.model is None:
            raise RuntimeError(f"{cls.__name__}.model must be set")
        return cls.model

    @classmethod
    def _payload_dict(cls, payload: Any, *, exclude_unset: bool) -> dict[str, Any]:
        if hasattr(payload, "model_dump"):
            dumped = payload.model_dump(exclude_unset=exclude_unset)
            return cast(dict[str, Any], dumped)
        if isinstance(payload, Mapping):
            return dict(payload)
        return dict(payload)

    @classmethod
    def _get_or_404(cls, db, entity_id: str):
        model = cls._require_model()
        entity = db.get(model, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail=cls.not_found_detail)
        # Treat soft-deleted rows as not found for get/update/delete.
        if cls.soft_delete_field:
            try:
                if getattr(entity, cls.soft_delete_field) == cls.soft_delete_value:
                    raise HTTPException(status_code=404, detail=cls.not_found_detail)
            except AttributeError:
                # Misconfigured soft_delete_field; fall back to returning the entity.
                pass
        return entity

    @classmethod
    def create(cls, db, payload):
        model = cls._require_model()
        entity = model(**cls._payload_dict(payload, exclude_unset=False))
        db.add(entity)
        db.commit()
        db.refresh(entity)
        return entity

    @classmethod
    def get(cls, db, entity_id: str):
        return cls._get_or_404(db, entity_id)

    @classmethod
    def update(cls, db, entity_id: str, payload):
        entity = cls._get_or_404(db, entity_id)
        for key, value in cls._payload_dict(payload, exclude_unset=True).items():
            setattr(entity, key, value)
        db.commit()
        db.refresh(entity)
        return entity

    @classmethod
    def delete(cls, db, entity_id: str):
        entity = cls._get_or_404(db, entity_id)
        if cls.soft_delete_field:
            setattr(entity, cls.soft_delete_field, cls.soft_delete_value)
        else:
            db.delete(entity)
        db.commit()
