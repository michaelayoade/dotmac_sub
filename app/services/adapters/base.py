"""Shared adapter contracts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class AdapterStatus(str, Enum):
    success = "success"
    error = "error"
    warning = "warning"
    queued = "queued"
    skipped = "skipped"


@dataclass
class AdapterResult:
    """Common adapter result shape."""

    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    status: AdapterStatus = AdapterStatus.success
    error_code: str | None = None

    @classmethod
    def ok(
        cls,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AdapterResult:
        return cls(
            success=True,
            message=message,
            data=data or {},
            status=AdapterStatus.success,
            **kwargs,
        )

    @classmethod
    def fail(
        cls,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
        **kwargs: Any,
    ) -> AdapterResult:
        return cls(
            success=False,
            message=message,
            data=data or {},
            status=AdapterStatus.error,
            error_code=error_code,
            **kwargs,
        )

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        operation: str,
        logger_: logging.Logger | None = None,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AdapterResult:
        log = logger_ or logger
        log.exception("%s failed", operation)
        return cls.fail(
            f"{operation} failed: {exc}",
            data=data,
            error_code=exc.__class__.__name__,
            **kwargs,
        )


class AdapterBase(Protocol):
    """Marker protocol for service adapters."""

    name: str


class AdapterRegistry:
    """Small explicit registry for service adapter instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterBase] = {}

    def register(self, adapter: AdapterBase, *, name: str | None = None) -> AdapterBase:
        adapter_name = name or getattr(adapter, "name", None)
        if not adapter_name:
            raise ValueError("Adapter name is required")
        self._adapters[str(adapter_name)] = adapter
        return adapter

    def get(self, name: str) -> AdapterBase | None:
        return self._adapters.get(name)

    def require(self, name: str) -> AdapterBase:
        adapter = self.get(name)
        if adapter is None:
            raise KeyError(f"Adapter '{name}' is not registered")
        return adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def clear(self) -> None:
        self._adapters.clear()
