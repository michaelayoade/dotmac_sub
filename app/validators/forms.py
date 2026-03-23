"""Shared form-parsing helpers for web route handlers.

These utilities parse raw form string values into typed Python objects,
raising ``ValueError`` with a human-readable message on bad input.  They
are used across multiple admin billing and provisioning route files to
avoid duplicating identical parsing logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, overload
from uuid import UUID


@overload
def parse_uuid(
    value: str | None, field: str, *, required: Literal[True] = ...
) -> UUID: ...


@overload
def parse_uuid(
    value: str | None, field: str, *, required: Literal[False]
) -> UUID | None: ...


def parse_uuid(value: str | None, field: str, *, required: bool = True) -> UUID | None:
    """Parse a string to UUID.

    Args:
        value: Raw form string (may be None or empty).
        field: Human-readable field name for error messages.
        required: If True (default), raise on missing value.

    Returns:
        Parsed UUID, or None when *required* is False and value is empty.

    Raises:
        ValueError: If value is missing (when required) or not a valid UUID.
    """
    if not value:
        if required:
            raise ValueError(f"{field} is required")
        return None
    return UUID(value)


def parse_decimal(
    value: str | None,
    field: str,
    default: Decimal | None = None,
) -> Decimal:
    """Parse a string to Decimal.

    Args:
        value: Raw form string (may be None or empty).
        field: Human-readable field name for error messages.
        default: Returned when value is empty and default is not None.

    Returns:
        Parsed Decimal value.

    Raises:
        ValueError: If value is missing (with no default) or not numeric.
    """
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid number") from exc


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-format string to a timezone-aware datetime.

    If the parsed value is naive (no tzinfo), UTC is assumed.

    Args:
        value: Raw form string in ISO 8601 format, or None/empty.

    Returns:
        Parsed datetime with timezone, or None if value is empty.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
