"""Shared adapter contracts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health Check Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthCheckResult:
    """Result of an adapter health check."""

    name: str
    healthy: bool
    message: str
    latency_ms: float | None = None
    dependencies_ok: bool = True

    def __bool__(self) -> bool:
        return self.healthy and self.dependencies_ok


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


@runtime_checkable
class AdapterBase(Protocol):
    """Marker protocol for service adapters.

    Adapters should declare:
        name: str - unique identifier for registry lookup
        depends_on: tuple[str, ...] - optional, names of adapters this one requires

    Adapters may implement:
        health_check() -> HealthCheckResult - for startup/runtime validation
    """

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

    def dependencies(self, name: str) -> tuple[str, ...]:
        """Return declared dependencies for an adapter."""
        adapter = self.get(name)
        if adapter is None:
            return ()
        deps = getattr(adapter, "depends_on", None)
        if deps is None:
            return ()
        return tuple(str(d) for d in deps)

    def startup_order(self) -> list[str]:
        """Return adapter names in dependency-safe startup order.

        Uses Kahn's algorithm for topological sort. Adapters with no
        dependencies come first, then those that depend on them, etc.
        """
        # Build adjacency and in-degree maps
        in_degree: dict[str, int] = {name: 0 for name in self._adapters}
        dependents: dict[str, list[str]] = {name: [] for name in self._adapters}

        for name in self._adapters:
            for dep in self.dependencies(name):
                if dep in self._adapters:
                    dependents[dep].append(name)
                    in_degree[name] += 1

        # Start with adapters that have no dependencies
        queue = [name for name, deg in in_degree.items() if deg == 0]
        result: list[str] = []

        while queue:
            # Sort for deterministic order among peers
            queue.sort()
            current = queue.pop(0)
            result.append(current)
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # If we didn't process all adapters, there's a cycle
        if len(result) != len(self._adapters):
            # Fall back to sorted order, log warning
            missing = set(self._adapters) - set(result)
            logger.warning(
                "Circular dependency detected among adapters: %s. Using fallback order.",
                ", ".join(sorted(missing)),
            )
            result.extend(sorted(missing))

        return result

    def health_check(self, name: str) -> HealthCheckResult:
        """Run health check for a single adapter."""
        import time

        adapter = self.get(name)
        if adapter is None:
            return HealthCheckResult(
                name=name,
                healthy=False,
                message=f"Adapter '{name}' is not registered",
            )

        # Check dependencies first
        deps_ok = True
        missing_deps: list[str] = []
        for dep in self.dependencies(name):
            if self.get(dep) is None:
                deps_ok = False
                missing_deps.append(dep)

        if not deps_ok:
            return HealthCheckResult(
                name=name,
                healthy=False,
                message=f"Missing dependencies: {', '.join(missing_deps)}",
                dependencies_ok=False,
            )

        # Run adapter's own health check if available
        health_fn = getattr(adapter, "health_check", None)
        if health_fn is None or not callable(health_fn):
            return HealthCheckResult(
                name=name,
                healthy=True,
                message="OK (no health_check method)",
                dependencies_ok=True,
            )

        start = time.monotonic()
        try:
            result = health_fn()
            latency_ms = (time.monotonic() - start) * 1000

            # Handle different return types
            if isinstance(result, HealthCheckResult):
                return HealthCheckResult(
                    name=name,
                    healthy=result.healthy,
                    message=result.message,
                    latency_ms=latency_ms,
                    dependencies_ok=True,
                )
            if isinstance(result, bool):
                return HealthCheckResult(
                    name=name,
                    healthy=result,
                    message="OK" if result else "Health check returned False",
                    latency_ms=latency_ms,
                    dependencies_ok=True,
                )
            if isinstance(result, tuple) and len(result) >= 2:
                return HealthCheckResult(
                    name=name,
                    healthy=bool(result[0]),
                    message=str(result[1]),
                    latency_ms=latency_ms,
                    dependencies_ok=True,
                )
            return HealthCheckResult(
                name=name,
                healthy=True,
                message="OK",
                latency_ms=latency_ms,
                dependencies_ok=True,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.warning("Health check failed for adapter %s: %s", name, exc)
            return HealthCheckResult(
                name=name,
                healthy=False,
                message=f"Health check error: {exc}",
                latency_ms=latency_ms,
                dependencies_ok=True,
            )

    def health_check_all(self) -> dict[str, HealthCheckResult]:
        """Run health checks for all adapters in startup order."""
        results: dict[str, HealthCheckResult] = {}
        for name in self.startup_order():
            results[name] = self.health_check(name)
        return results

    def is_healthy(self) -> bool:
        """Return True if all adapters pass health checks."""
        return all(r.healthy for r in self.health_check_all().values())
