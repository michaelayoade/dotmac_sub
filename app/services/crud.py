"""Generic CRUD manager utilities for service-layer boilerplate reduction."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Generic, TypeVar, cast

from fastapi import HTTPException

from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

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
    def _get_or_404(cls, db, entity_id: str, *, include_inactive: bool = False):
        model = cls._require_model()
        entity = db.get(model, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail=cls.not_found_detail)
        # Treat soft-deleted rows as not found for get/update/delete,
        # unless the caller explicitly requests inactive rows (e.g. for
        # viewing newly-created ONTs that haven't been assigned yet).
        if cls.soft_delete_field and not include_inactive:
            try:
                if getattr(entity, cls.soft_delete_field) == cls.soft_delete_value:
                    raise HTTPException(status_code=404, detail=cls.not_found_detail)
            except AttributeError:
                # Misconfigured soft_delete_field; fall back to returning the entity.
                pass
        return entity

    @classmethod
    def create(cls, db, payload, *, commit: bool = True):
        """Create a new entity from the given payload.

        Args:
            db: Database session.
            payload: Data to create the entity with (Pydantic model or dict).
            commit: If True (default), commits the transaction.
                   If False, only flushes (caller controls transaction).

        Returns:
            The created entity.
        """
        model = cls._require_model()
        entity = model(**cls._payload_dict(payload, exclude_unset=False))
        db.add(entity)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(entity)
        return entity

    @classmethod
    def get(cls, db, entity_id: str):
        return cls._get_or_404(db, entity_id)

    @classmethod
    def get_including_inactive(cls, db, entity_id: str):
        """Like get(), but returns entities even if soft-deleted / inactive."""
        return cls._get_or_404(db, entity_id, include_inactive=True)

    @classmethod
    def update(cls, db, entity_id: str, payload, *, commit: bool = True):
        """Update an existing entity with the given payload.

        Args:
            db: Database session.
            entity_id: ID of the entity to update.
            payload: Data to update the entity with (Pydantic model or dict).
            commit: If True (default), commits the transaction.
                   If False, only flushes (caller controls transaction).

        Returns:
            The updated entity.

        Raises:
            HTTPException: 404 if entity not found.
        """
        entity = cls._get_or_404(db, entity_id)
        for key, value in cls._payload_dict(payload, exclude_unset=True).items():
            setattr(entity, key, value)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(entity)
        return entity

    @classmethod
    def delete(cls, db, entity_id: str, *, commit: bool = True):
        """Delete an entity (soft or hard delete based on configuration).

        Args:
            db: Database session.
            entity_id: ID of the entity to delete.
            commit: If True (default), commits the transaction.
                   If False, only flushes (caller controls transaction).

        Raises:
            HTTPException: 404 if entity not found.
        """
        entity = cls._get_or_404(db, entity_id)
        if cls.soft_delete_field:
            setattr(entity, cls.soft_delete_field, cls.soft_delete_value)
        else:
            db.delete(entity)
        if commit:
            db.commit()
        else:
            db.flush()
