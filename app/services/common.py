"""Common helper functions for service layer.

This module provides reusable utilities for:
- UUID handling
- Query ordering and pagination
- Enum validation
- Entity retrieval with 404 handling
- Monetary calculations
- Standard filtering patterns
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, TypeVar

from fastapi import HTTPException

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

T = TypeVar("T")


def coerce_uuid(value):
    """Convert value to UUID, returning None if value is None."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def apply_ordering(query, order_by: str, order_dir: str, allowed_columns: dict):
    """Apply ordering to a query with validation.

    Args:
        query: SQLAlchemy query object
        order_by: Column name to order by
        order_dir: Direction ('asc' or 'desc')
        allowed_columns: Dict mapping column names to SQLAlchemy columns

    Returns:
        Query with ordering applied

    Raises:
        HTTPException: 400 if order_by is not in allowed_columns
    """
    if order_by not in allowed_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order_by. Allowed: {', '.join(sorted(allowed_columns))}",
        )
    column = allowed_columns[order_by]
    if order_dir == "desc":
        return query.order_by(column.desc())
    return query.order_by(column.asc())


def apply_pagination(query, limit: int, offset: int):
    """Apply pagination to a query.

    Args:
        query: SQLAlchemy query object
        limit: Maximum number of results
        offset: Number of results to skip

    Returns:
        Query with pagination applied
    """
    return query.limit(limit).offset(offset)


def validate_enum(value, enum_cls, label: str):
    """Validate and convert a value to an enum member.

    Args:
        value: Value to validate (can be None)
        enum_cls: Enum class to validate against
        label: Human-readable label for error messages

    Returns:
        Enum member or None if value is None

    Raises:
        HTTPException: 400 if value is not a valid enum member
    """
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


def apply_is_active_filter(query, model, is_active: bool | None):
    """Apply standard is_active filter logic.

    If is_active is None, defaults to filtering for active records only.

    Args:
        query: SQLAlchemy query object
        model: SQLAlchemy model class with is_active column
        is_active: Filter value (None defaults to True)

    Returns:
        Query with is_active filter applied
    """
    if is_active is None:
        return query.filter(model.is_active.is_(True))
    return query.filter(model.is_active == is_active)


def get_or_404(db: Session, model: type[T], id: str, detail: str | None = None, **options) -> T:
    """Get entity by ID or raise 404.

    Args:
        db: Database session
        model: SQLAlchemy model class
        id: Entity ID (string or UUID)
        detail: Custom error message (defaults to "{ModelName} not found")
        **options: Additional options passed to db.get() (e.g., options=[selectinload(...)])

    Returns:
        Entity instance

    Raises:
        HTTPException: 404 if entity not found
    """
    entity = db.get(model, coerce_uuid(id), **options)
    if not entity:
        raise HTTPException(
            status_code=404,
            detail=detail or f"{model.__name__} not found"
        )
    return entity


def get_by_id(db: Session, model: type[T], value, **kwargs) -> T | None:
    """Get entity by ID, returning None if not found or value is None.

    Args:
        db: Database session
        model: SQLAlchemy model class
        value: Entity ID (can be None)
        **kwargs: Additional options passed to db.get()

    Returns:
        Entity instance or None
    """
    if value is None:
        return None
    return db.get(model, coerce_uuid(value), **kwargs)


def ensure_exists(db: Session, model: type[T], id: str, detail: str) -> T:
    """Ensure entity exists, raise 404 if not.

    Args:
        db: Database session
        model: SQLAlchemy model class
        id: Entity ID
        detail: Error message if not found

    Returns:
        Entity instance

    Raises:
        HTTPException: 404 if entity not found
    """
    entity = get_by_id(db, model, id)
    if not entity:
        raise HTTPException(status_code=404, detail=detail)
    return entity


def round_money(value: Decimal | int | float | str) -> Decimal:
    """Round monetary value to 2 decimal places using banker's rounding.

    Args:
        value: Monetary value to round

    Returns:
        Decimal rounded to 2 decimal places
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate_positive_decimal(value: Decimal | None, label: str) -> Decimal | None:
    """Validate that a decimal value is positive.

    Args:
        value: Decimal value to validate (can be None)
        label: Human-readable label for error messages

    Returns:
        The value if valid or None

    Raises:
        HTTPException: 400 if value is negative
    """
    if value is not None and value < 0:
        raise HTTPException(status_code=400, detail=f"{label} cannot be negative")
    return value
