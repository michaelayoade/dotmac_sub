"""Provisioning step result types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def _json_safe_step_data(value: Any) -> Any:
    """Normalize StepResult data to JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_step_data(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _json_safe_step_data(value.model_dump())
    if hasattr(value, "_asdict") and callable(value._asdict):
        return _json_safe_step_data(value._asdict())
    if isinstance(value, Mapping):
        items = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        return {key: _json_safe_step_data(item) for key, item in items}
    if isinstance(value, set):
        normalized_items = [_json_safe_step_data(item) for item in value]
        return sorted(normalized_items, key=lambda item: repr(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe_step_data(item) for item in value]
    return str(value)


@dataclass
class StepResult:
    """Result of a single provisioning operation."""

    step_name: str
    success: bool
    message: str
    duration_ms: int = 0
    critical: bool = True
    skipped: bool = False
    waiting: bool = False
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            normalized = _json_safe_step_data(self.data)
            self.data = (
                normalized if isinstance(normalized, dict) else {"value": normalized}
            )
