"""Shared utilities for network services.

This module provides common helper functions used across network services
to avoid duplication and ensure consistent behavior.
"""

from __future__ import annotations

from typing import Any


def first_present(*values: Any, exclude_empty_list: bool = False) -> Any:
    """Return the first non-empty value from the arguments.

    Args:
        *values: Values to check in order.
        exclude_empty_list: If True, also treat [] as empty (default False).

    Returns:
        The first value that is not None or empty string.
        Returns None if all values are empty.

    Note:
        - Preserves False and 0 (they are valid values)
        - Empty string "" is treated as empty
        - Empty list [] is only excluded if exclude_empty_list=True
    """
    empty = (None, "", []) if exclude_empty_list else (None, "")
    for value in values:
        if value not in empty:
            return value
    return None


def first_present_enum(*values: Any, exclude_empty_list: bool = False) -> Any:
    """Like first_present() but unwraps enum values.

    If a value has a .value attribute (like an Enum), returns .value instead.
    """
    empty = (None, "", []) if exclude_empty_list else (None, "")
    for value in values:
        if value not in empty:
            return getattr(value, "value", value)
    return None


def first_key_present(item: dict[str, Any], *keys: str) -> Any:
    """Return the value of the first key that exists and is not None.

    Args:
        item: Dictionary to search.
        *keys: Keys to check in order.

    Returns:
        The value of the first key found with a non-None value.
        Returns None if no key has a value.
    """
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None
