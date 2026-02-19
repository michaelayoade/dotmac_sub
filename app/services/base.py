"""Base CRUD service for common operations.

This module provides a generic base class for CRUD operations that can be
inherited by specific services to reduce code duplication and ensure
consistent behavior across the codebase.
"""

from typing import Any, Generic, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.common import coerce_uuid

T = TypeVar("T")


class BaseCRUDService(Generic[T]):
    """Generic base class for CRUD operations.

    Provides standard create, read, update, delete operations that can be
    customized by subclasses. Reduces boilerplate and ensures consistent
    error handling across services.

    Attributes:
        model_class: The SQLAlchemy model class to operate on
        not_found_message: Error message when resource is not found
        id_field: Name of the ID field (default: 'id')
    """

    model_class: type[T]
    not_found_message: str = "Resource not found"
    id_field: str = "id"

    @classmethod
    def get(cls, db: Session, resource_id: str | UUID) -> T:
        """Retrieve a resource by ID.

        Args:
            db: Database session
            resource_id: The resource ID (string or UUID)

        Returns:
            The resource instance

        Raises:
            HTTPException: 404 if resource not found
        """
        obj = db.get(cls.model_class, coerce_uuid(resource_id))
        if not obj:
            raise HTTPException(status_code=404, detail=cls.not_found_message)
        return obj

    @classmethod
    def get_or_none(cls, db: Session, resource_id: str | UUID) -> T | None:
        """Retrieve a resource by ID, returning None if not found.

        Args:
            db: Database session
            resource_id: The resource ID (string or UUID)

        Returns:
            The resource instance or None
        """
        return db.get(cls.model_class, coerce_uuid(resource_id))

    @classmethod
    def create(cls, db: Session, data: dict[str, Any]) -> T:
        """Create a new resource.

        Args:
            db: Database session
            data: Dictionary of field values

        Returns:
            The created resource instance
        """
        obj = cls.model_class(**data)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    @classmethod
    def update(
        cls,
        db: Session,
        resource_id: str | UUID,
        data: dict[str, Any],
        exclude_none: bool = True,
    ) -> T:
        """Update an existing resource.

        Args:
            db: Database session
            resource_id: The resource ID
            data: Dictionary of fields to update
            exclude_none: If True, skip None values in data

        Returns:
            The updated resource instance

        Raises:
            HTTPException: 404 if resource not found
        """
        obj = cls.get(db, resource_id)
        for key, value in data.items():
            if exclude_none and value is None:
                continue
            if hasattr(obj, key):
                setattr(obj, key, value)
        db.commit()
        db.refresh(obj)
        return obj

    @classmethod
    def delete(cls, db: Session, resource_id: str | UUID) -> None:
        """Delete a resource by ID.

        Args:
            db: Database session
            resource_id: The resource ID

        Raises:
            HTTPException: 404 if resource not found
        """
        obj = cls.get(db, resource_id)
        db.delete(obj)
        db.commit()

    @classmethod
    def soft_delete(
        cls,
        db: Session,
        resource_id: str | UUID,
        active_field: str = "is_active",
    ) -> T:
        """Soft delete a resource by setting is_active to False.

        Args:
            db: Database session
            resource_id: The resource ID
            active_field: Name of the active flag field

        Returns:
            The updated resource instance

        Raises:
            HTTPException: 404 if resource not found
        """
        obj = cls.get(db, resource_id)
        setattr(obj, active_field, False)
        db.commit()
        db.refresh(obj)
        return obj

    @classmethod
    def list_all(
        cls,
        db: Session,
        filters: dict[str, Any] | None = None,
        is_active: bool | None = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[T]:
        """List resources with optional filtering.

        Args:
            db: Database session
            filters: Dictionary of field=value filters
            is_active: Filter by is_active field (None to skip)
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of matching resources
        """
        query = db.query(cls.model_class)

        if filters:
            for field, value in filters.items():
                if hasattr(cls.model_class, field) and value is not None:
                    query = query.filter(getattr(cls.model_class, field) == value)

        if is_active is not None:
            is_active_attr = getattr(cls.model_class, "is_active", None)
            if is_active_attr is not None:
                query = query.filter(is_active_attr == is_active)

        return query.offset(offset).limit(limit).all()

    @classmethod
    def count(
        cls,
        db: Session,
        filters: dict[str, Any] | None = None,
        is_active: bool | None = True,
    ) -> int:
        """Count resources matching filters.

        Args:
            db: Database session
            filters: Dictionary of field=value filters
            is_active: Filter by is_active field (None to skip)

        Returns:
            Count of matching resources
        """
        query = db.query(cls.model_class)

        if filters:
            for field, value in filters.items():
                if hasattr(cls.model_class, field) and value is not None:
                    query = query.filter(getattr(cls.model_class, field) == value)

        if is_active is not None:
            is_active_attr = getattr(cls.model_class, "is_active", None)
            if is_active_attr is not None:
                query = query.filter(is_active_attr == is_active)

        return query.count()

    @classmethod
    def exists(cls, db: Session, resource_id: str | UUID) -> bool:
        """Check if a resource exists.

        Args:
            db: Database session
            resource_id: The resource ID

        Returns:
            True if resource exists, False otherwise
        """
        obj = db.get(cls.model_class, coerce_uuid(resource_id))
        return obj is not None
