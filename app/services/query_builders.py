"""Reusable query-builder helpers for service-layer list filtering."""

from __future__ import annotations


def apply_optional_equals(query, filters: dict):
    """Apply equality filters when values are not None."""
    for column, value in filters.items():
        if value is not None:
            query = query.filter(column == value)
    return query


def apply_optional_ilike(query, filters: dict):
    """Apply case-insensitive contains filters when values are non-empty."""
    for column, value in filters.items():
        if value:
            query = query.filter(column.ilike(f"%{value}%"))
    return query


def apply_active_state(query, column, is_active: bool | None, *, default_active: bool = True):
    """Apply standard active-state filtering against a model column."""
    if is_active is None:
        if default_active:
            return query.filter(column.is_(True))
        return query
    return query.filter(column == is_active)
