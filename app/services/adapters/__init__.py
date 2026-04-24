"""Shared adapter interfaces and result types."""

from app.services.adapters.base import (
    AdapterBase,
    AdapterRegistry,
    AdapterResult,
    AdapterStatus,
    HealthCheckResult,
)

adapter_registry = AdapterRegistry()

__all__ = [
    "AdapterBase",
    "AdapterRegistry",
    "AdapterResult",
    "AdapterStatus",
    "HealthCheckResult",
    "adapter_registry",
]
