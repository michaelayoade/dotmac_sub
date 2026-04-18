"""ONT Status Adapter - Unified status fetching across SNMP and TR-069.

This adapter provides a single interface for getting ONT status and optical
metrics regardless of the underlying polling mechanism (OLT SNMP vs ACS TR-069).

For new code, use:
    from app.services.network.ont_status_adapter import get_status_provider
    provider = get_status_provider()
    status = provider.get_status(db, ont)
    metrics = provider.get_optical_metrics(db, ont)

The adapter supports three modes:
- "snmp": Use OLT SNMP polling only
- "tr069": Use TR-069/ACS only
- "auto" (default): Try SNMP first, fall back to TR-069 if available
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.models.network import OntAcsStatus, OntStatusSource, OntUnit, OnuOnlineStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


class StatusProviderMode(str, Enum):
    """Status provider selection mode."""

    auto = "auto"
    snmp = "snmp"
    tr069 = "tr069"


@dataclass(frozen=True)
class OpticalMetrics:
    """Unified optical signal metrics from any source."""

    # Core signal levels (dBm)
    olt_rx_dbm: float | None = None
    onu_rx_dbm: float | None = None
    onu_tx_dbm: float | None = None

    # DDM diagnostics
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None

    # Distance/attenuation
    distance_m: int | None = None

    # Metadata
    source: str = "unknown"
    fetched_at: datetime | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "olt_rx_dbm": self.olt_rx_dbm,
            "onu_rx_dbm": self.onu_rx_dbm,
            "onu_tx_dbm": self.onu_tx_dbm,
            "temperature_c": self.temperature_c,
            "voltage_v": self.voltage_v,
            "bias_current_ma": self.bias_current_ma,
            "distance_m": self.distance_m,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }

    @property
    def has_signal_data(self) -> bool:
        """Check if any signal data is present."""
        return any([
            self.olt_rx_dbm is not None,
            self.onu_rx_dbm is not None,
            self.onu_tx_dbm is not None,
        ])


@dataclass(frozen=True)
class OntStatusResult:
    """Unified ONT status from any source."""

    online_status: OnuOnlineStatus
    acs_status: OntAcsStatus
    status_source: OntStatusSource
    acs_last_inform_at: datetime | None = None
    resolved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Optional optical metrics if fetched together
    optical_metrics: OpticalMetrics | None = None

    # Error information if fetch failed
    error: str | None = None

    @property
    def is_online(self) -> bool:
        """Check if ONT is online."""
        return self.online_status == OnuOnlineStatus.online

    @property
    def success(self) -> bool:
        """Check if status fetch succeeded."""
        return self.error is None


# ---------------------------------------------------------------------------
# Protocol Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class OntStatusProvider(Protocol):
    """Protocol for ONT status providers.

    Implementations fetch ONT status and optical metrics from different sources
    (SNMP via OLT, TR-069 via ACS, or both).
    """

    @property
    def name(self) -> str:
        """Provider name for logging and identification."""
        ...

    def get_status(
        self,
        db: "Session",
        ont: OntUnit,
        *,
        include_optical: bool = False,
    ) -> OntStatusResult:
        """Get current ONT status.

        Args:
            db: Database session
            ont: ONT unit to check
            include_optical: Also fetch optical metrics if available

        Returns:
            OntStatusResult with online status and optional optical metrics
        """
        ...

    def get_optical_metrics(
        self,
        db: "Session",
        ont: OntUnit,
    ) -> OpticalMetrics:
        """Get current optical signal metrics.

        Args:
            db: Database session
            ont: ONT unit to query

        Returns:
            OpticalMetrics with signal levels and diagnostics
        """
        ...

    def supports_ont(self, ont: OntUnit) -> bool:
        """Check if this provider can handle the given ONT.

        Args:
            ont: ONT unit to check

        Returns:
            True if this provider can fetch status for this ONT
        """
        ...


# ---------------------------------------------------------------------------
# SNMP Status Provider
# ---------------------------------------------------------------------------


class SnmpStatusProvider:
    """Fetches ONT status via OLT SNMP polling.

    Uses the existing olt_polling infrastructure to get status from
    cached SNMP poll results stored in the database.
    """

    @property
    def name(self) -> str:
        return "snmp"

    def supports_ont(self, ont: OntUnit) -> bool:
        """SNMP provider requires ONT to be linked to an OLT."""
        return bool(getattr(ont, "olt_device_id", None))

    def get_status(
        self,
        db: "Session",
        ont: OntUnit,
        *,
        include_optical: bool = False,
    ) -> OntStatusResult:
        """Get status from cached SNMP poll data."""
        from app.services.network.ont_status import (
            resolve_acs_online_window_minutes_for_model,
            resolve_acs_status,
        )

        now = datetime.now(UTC)

        # Get OLT status from ONT record (updated by polling)
        olt_status = getattr(ont, "online_status", None) or OnuOnlineStatus.unknown

        # Resolve ACS status for combined view
        acs_last_inform = getattr(ont, "acs_last_inform_at", None)
        managed = bool(
            getattr(ont, "tr069_acs_server_id", None)
            or getattr(ont, "tr069_acs_server", None)
        )
        window_minutes = resolve_acs_online_window_minutes_for_model(ont)
        acs_status = resolve_acs_status(
            acs_last_inform_at=acs_last_inform,
            managed=managed,
            now=now,
            online_window_minutes=window_minutes,
        )

        # Determine effective status
        if olt_status == OnuOnlineStatus.online:
            effective_status = OnuOnlineStatus.online
            source = OntStatusSource.olt
        elif olt_status == OnuOnlineStatus.offline:
            effective_status = OnuOnlineStatus.offline
            source = OntStatusSource.olt
        elif acs_status == OntAcsStatus.online:
            effective_status = OnuOnlineStatus.online
            source = OntStatusSource.acs
        else:
            effective_status = OnuOnlineStatus.unknown
            source = OntStatusSource.derived

        optical = None
        if include_optical:
            optical = self.get_optical_metrics(db, ont)

        return OntStatusResult(
            online_status=effective_status,
            acs_status=acs_status,
            status_source=source,
            acs_last_inform_at=acs_last_inform,
            resolved_at=now,
            optical_metrics=optical,
        )

    def get_optical_metrics(
        self,
        db: "Session",
        ont: OntUnit,
    ) -> OpticalMetrics:
        """Get optical metrics from cached SNMP poll data."""
        return OpticalMetrics(
            olt_rx_dbm=getattr(ont, "olt_rx_signal_dbm", None),
            onu_rx_dbm=getattr(ont, "onu_rx_signal_dbm", None),
            onu_tx_dbm=getattr(ont, "onu_tx_signal_dbm", None),
            temperature_c=getattr(ont, "temperature_c", None),
            voltage_v=getattr(ont, "voltage_v", None),
            bias_current_ma=getattr(ont, "bias_current_ma", None),
            distance_m=getattr(ont, "distance_meters", None),
            source="snmp",
            fetched_at=getattr(ont, "last_polled_at", None),
        )


# ---------------------------------------------------------------------------
# TR-069 Status Provider
# ---------------------------------------------------------------------------


class Tr069StatusProvider:
    """Fetches ONT status via TR-069/ACS.

    Uses GenieACS to get status and optical metrics directly from the CPE
    device via TR-069 protocol.
    """

    @property
    def name(self) -> str:
        return "tr069"

    def supports_ont(self, ont: OntUnit) -> bool:
        """TR-069 provider requires ONT to have ACS configuration."""
        # Check for direct ACS link or OLT-level ACS
        if getattr(ont, "tr069_acs_server_id", None):
            return True
        if getattr(ont, "tr069_acs_server", None):
            return True

        # Check OLT for ACS config
        olt = getattr(ont, "olt_device", None)
        if olt and (
            getattr(olt, "tr069_acs_server_id", None)
            or getattr(olt, "tr069_acs_server", None)
        ):
            return True

        # Has recent ACS contact
        return bool(getattr(ont, "acs_last_inform_at", None))

    def get_status(
        self,
        db: "Session",
        ont: OntUnit,
        *,
        include_optical: bool = False,
    ) -> OntStatusResult:
        """Get status from ACS/TR-069."""
        from app.services.network.ont_status import (
            resolve_acs_online_window_minutes_for_model,
            resolve_acs_status,
        )

        now = datetime.now(UTC)

        # Get ACS status from cached data
        acs_last_inform = getattr(ont, "acs_last_inform_at", None)
        window_minutes = resolve_acs_online_window_minutes_for_model(ont)
        acs_status = resolve_acs_status(
            acs_last_inform_at=acs_last_inform,
            managed=True,
            now=now,
            online_window_minutes=window_minutes,
        )

        # Map ACS status to online status
        if acs_status == OntAcsStatus.online:
            effective_status = OnuOnlineStatus.online
        elif acs_status == OntAcsStatus.stale:
            effective_status = OnuOnlineStatus.offline
        else:
            effective_status = OnuOnlineStatus.unknown

        optical = None
        if include_optical:
            optical = self.get_optical_metrics(db, ont)

        return OntStatusResult(
            online_status=effective_status,
            acs_status=acs_status,
            status_source=OntStatusSource.acs,
            acs_last_inform_at=acs_last_inform,
            resolved_at=now,
            optical_metrics=optical,
        )

    def get_optical_metrics(
        self,
        db: "Session",
        ont: OntUnit,
    ) -> OpticalMetrics:
        """Get optical metrics via TR-069.

        This fetches from cached GenieACS device data. For real-time data,
        use fetch_optical_metrics_live().
        """
        # Try to get cached TR-069 parameters
        tr069_device = self._get_tr069_device(db, ont)
        if not tr069_device:
            return OpticalMetrics(source="tr069", fetched_at=datetime.now(UTC))

        # Extract optical parameters from cached device data
        params = getattr(tr069_device, "cached_parameters", None) or {}

        return OpticalMetrics(
            onu_rx_dbm=self._parse_dbm(params.get("optical.signal_level")),
            onu_tx_dbm=self._parse_dbm(params.get("optical.transmit_level")),
            source="tr069",
            fetched_at=getattr(tr069_device, "last_inform_at", None),
        )

    def fetch_optical_metrics_live(
        self,
        db: "Session",
        ont: OntUnit,
        *,
        timeout_seconds: int = 30,
    ) -> OpticalMetrics:
        """Fetch live optical metrics via TR-069 RPC.

        This triggers an actual parameter fetch from the device.
        Use for on-demand diagnostics, not bulk polling.
        """
        from app.services.genieacs import GenieACSClient
        from app.services.network.tr069_paths import resolve_parameters

        tr069_device = self._get_tr069_device(db, ont)
        if not tr069_device:
            logger.warning("No TR-069 device found for ONT %s", ont.id)
            return OpticalMetrics(source="tr069", fetched_at=datetime.now(UTC))

        device_id = getattr(tr069_device, "device_id", None)
        acs_server = self._get_acs_server(ont)
        if not device_id or not acs_server:
            return OpticalMetrics(source="tr069", fetched_at=datetime.now(UTC))

        try:
            client = GenieACSClient(
                base_url=acs_server.api_url,
                username=acs_server.username,
                password=acs_server.password,
            )

            # Get optical parameter paths for this device
            data_model = getattr(tr069_device, "data_model_root", "Device")
            optical_params = resolve_parameters(
                ["optical.signal_level", "optical.transmit_level"],
                data_model_root=data_model,
            )

            # Fetch parameters
            client.get_parameter_values(
                device_id,
                list(optical_params.values()),
                connection_request=True,
            )

            # Re-fetch cached data after RPC
            # Note: GenieACS may take time to update cache
            return self.get_optical_metrics(db, ont)

        except Exception as exc:
            logger.error(
                "Failed to fetch TR-069 optical metrics for ONT %s: %s",
                ont.id,
                exc,
            )
            return OpticalMetrics(
                source="tr069",
                fetched_at=datetime.now(UTC),
            )

    def _get_tr069_device(self, db: "Session", ont: OntUnit):
        """Get the TR-069 CPE device record for this ONT."""
        from sqlalchemy import select

        from app.models.tr069 import Tr069CpeDevice

        serial = getattr(ont, "serial_number", None)
        if not serial:
            return None

        # Normalize serial for matching
        import re

        normalized = re.sub(r"[^A-Za-z0-9]", "", serial).upper()

        stmt = select(Tr069CpeDevice).where(
            Tr069CpeDevice.serial_number.ilike(f"%{normalized[-8:]}%")
        )
        return db.scalars(stmt).first()

    def _get_acs_server(self, ont: OntUnit):
        """Get the ACS server for this ONT."""
        # Direct link
        acs = getattr(ont, "tr069_acs_server", None)
        if acs:
            return acs

        # Via OLT
        olt = getattr(ont, "olt_device", None)
        if olt:
            return getattr(olt, "tr069_acs_server", None)

        return None

    @staticmethod
    def _parse_dbm(value) -> float | None:
        """Parse dBm value from TR-069 parameter."""
        if value is None:
            return None
        try:
            # Handle string or numeric
            dbm = float(str(value).strip())
            # Sanity check: valid optical range
            if -50 <= dbm <= 10:
                return round(dbm, 2)
            return None
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Composite Status Provider
# ---------------------------------------------------------------------------


class CompositeStatusProvider:
    """Combines multiple providers with automatic fallback.

    Tries providers in order until one succeeds. Default order:
    1. SNMP (if ONT has OLT link and recent poll data)
    2. TR-069 (if ONT has ACS link)
    """

    def __init__(
        self,
        providers: list[OntStatusProvider] | None = None,
    ):
        self._providers = providers or [
            SnmpStatusProvider(),
            Tr069StatusProvider(),
        ]

    @property
    def name(self) -> str:
        return "composite"

    def supports_ont(self, ont: OntUnit) -> bool:
        """Check if any provider supports this ONT."""
        return any(p.supports_ont(ont) for p in self._providers)

    def get_status(
        self,
        db: "Session",
        ont: OntUnit,
        *,
        include_optical: bool = False,
    ) -> OntStatusResult:
        """Get status from first available provider.

        Tries each provider in order. If SNMP returns unknown status,
        falls back to TR-069 if available.
        """
        errors = []

        for provider in self._providers:
            if not provider.supports_ont(ont):
                continue

            try:
                result = provider.get_status(db, ont, include_optical=include_optical)

                # If we got a definitive status, return it
                if result.online_status != OnuOnlineStatus.unknown:
                    return result

                # Unknown status - try next provider
                logger.debug(
                    "Provider %s returned unknown status for ONT %s, trying next",
                    provider.name,
                    ont.id,
                )

            except Exception as exc:
                logger.warning(
                    "Provider %s failed for ONT %s: %s",
                    provider.name,
                    ont.id,
                    exc,
                )
                errors.append(f"{provider.name}: {exc}")

        # No provider succeeded with definitive status
        return OntStatusResult(
            online_status=OnuOnlineStatus.unknown,
            acs_status=OntAcsStatus.unknown,
            status_source=OntStatusSource.derived,
            resolved_at=datetime.now(UTC),
            error="; ".join(errors) if errors else "No provider available",
        )

    def get_optical_metrics(
        self,
        db: "Session",
        ont: OntUnit,
    ) -> OpticalMetrics:
        """Get optical metrics from first available provider with data."""
        for provider in self._providers:
            if not provider.supports_ont(ont):
                continue

            try:
                metrics = provider.get_optical_metrics(db, ont)
                if metrics.has_signal_data:
                    return metrics
            except Exception as exc:
                logger.debug(
                    "Provider %s failed to get optical metrics for ONT %s: %s",
                    provider.name,
                    ont.id,
                    exc,
                )

        return OpticalMetrics(source="none", fetched_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------


_provider_cache: dict[StatusProviderMode, OntStatusProvider] = {}


def get_status_provider(
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusProvider:
    """Get the appropriate status provider for the specified mode.

    Args:
        mode: Provider selection mode:
            - "auto": Composite provider with fallback (default)
            - "snmp": SNMP-only provider
            - "tr069": TR-069-only provider

    Returns:
        OntStatusProvider implementation
    """
    if isinstance(mode, str):
        mode = StatusProviderMode(mode.lower())

    if mode in _provider_cache:
        return _provider_cache[mode]

    if mode == StatusProviderMode.snmp:
        provider = SnmpStatusProvider()
    elif mode == StatusProviderMode.tr069:
        provider = Tr069StatusProvider()
    else:
        provider = CompositeStatusProvider()

    _provider_cache[mode] = provider
    return provider


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def get_ont_status(
    db: "Session",
    ont: OntUnit,
    *,
    include_optical: bool = False,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    """Get ONT status using the specified provider mode.

    This is a convenience wrapper around get_status_provider().

    Args:
        db: Database session
        ont: ONT unit to check
        include_optical: Also fetch optical metrics
        mode: Provider mode (auto, snmp, tr069)

    Returns:
        OntStatusResult with status and optional optical metrics
    """
    provider = get_status_provider(mode)
    return provider.get_status(db, ont, include_optical=include_optical)


def get_ont_optical_metrics(
    db: "Session",
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OpticalMetrics:
    """Get ONT optical metrics using the specified provider mode.

    Args:
        db: Database session
        ont: ONT unit to query
        mode: Provider mode (auto, snmp, tr069)

    Returns:
        OpticalMetrics with signal levels and diagnostics
    """
    provider = get_status_provider(mode)
    return provider.get_optical_metrics(db, ont)


def refresh_ont_status(
    db: "Session",
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    """Refresh ONT status and update database record.

    Fetches current status and applies it to the ONT model.

    Args:
        db: Database session
        ont: ONT unit to refresh
        mode: Provider mode

    Returns:
        OntStatusResult with updated status
    """
    from app.services.network.ont_status import apply_status_snapshot, OntStatusSnapshot

    result = get_ont_status(db, ont, include_optical=True, mode=mode)

    # Apply status to model
    snapshot = OntStatusSnapshot(
        olt_status=result.online_status,
        acs_status=result.acs_status,
        acs_last_inform_at=result.acs_last_inform_at,
        effective_status=result.online_status,
        effective_status_source=result.status_source,
        status_resolved_at=result.resolved_at,
    )
    apply_status_snapshot(ont, snapshot)

    # Apply optical metrics if present
    if result.optical_metrics and result.optical_metrics.has_signal_data:
        metrics = result.optical_metrics
        if metrics.olt_rx_dbm is not None:
            ont.olt_rx_signal_dbm = metrics.olt_rx_dbm
        if metrics.onu_rx_dbm is not None:
            ont.onu_rx_signal_dbm = metrics.onu_rx_dbm
        if metrics.onu_tx_dbm is not None:
            ont.onu_tx_signal_dbm = metrics.onu_tx_dbm
        if metrics.temperature_c is not None:
            ont.temperature_c = metrics.temperature_c
        if metrics.voltage_v is not None:
            ont.voltage_v = metrics.voltage_v
        if metrics.bias_current_ma is not None:
            ont.bias_current_ma = metrics.bias_current_ma
        if metrics.distance_m is not None:
            ont.distance_meters = metrics.distance_m

    return result


# ---------------------------------------------------------------------------
# Bulk Operations
# ---------------------------------------------------------------------------


@dataclass
class BulkStatusResult:
    """Result of bulk status fetch operation."""

    results: dict[str, OntStatusResult]  # ont_id -> result
    success_count: int = 0
    error_count: int = 0
    duration_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.results)


def get_bulk_ont_status(
    db: "Session",
    onts: list[OntUnit],
    *,
    include_optical: bool = False,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> BulkStatusResult:
    """Get status for multiple ONTs efficiently.

    For SNMP mode, this uses cached database values (fast).
    For TR-069 mode, this also uses cached ACS data.

    Args:
        db: Database session
        onts: List of ONT units to check
        include_optical: Include optical metrics
        mode: Provider mode

    Returns:
        BulkStatusResult with individual results per ONT
    """
    import time

    start = time.monotonic()
    provider = get_status_provider(mode)

    results: dict[str, OntStatusResult] = {}
    success_count = 0
    error_count = 0

    for ont in onts:
        try:
            result = provider.get_status(db, ont, include_optical=include_optical)
            results[str(ont.id)] = result
            if result.success:
                success_count += 1
            else:
                error_count += 1
        except Exception as exc:
            logger.error("Failed to get status for ONT %s: %s", ont.id, exc)
            results[str(ont.id)] = OntStatusResult(
                online_status=OnuOnlineStatus.unknown,
                acs_status=OntAcsStatus.unknown,
                status_source=OntStatusSource.derived,
                error=str(exc),
            )
            error_count += 1

    duration_ms = int((time.monotonic() - start) * 1000)

    return BulkStatusResult(
        results=results,
        success_count=success_count,
        error_count=error_count,
        duration_ms=duration_ms,
    )


def refresh_olt_ont_status(
    db: "Session",
    olt: "OLTDevice",
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> BulkStatusResult:
    """Refresh status for all ONTs on an OLT.

    This is a convenience function for OLT-level status refresh.

    Args:
        db: Database session
        olt: OLT device
        mode: Provider mode

    Returns:
        BulkStatusResult with results for all ONTs
    """
    from sqlalchemy import select

    # Get all active ONTs for this OLT
    stmt = select(OntUnit).where(
        OntUnit.olt_device_id == olt.id,
        OntUnit.is_active.is_(True),
    )
    onts = list(db.scalars(stmt).all())

    if not onts:
        return BulkStatusResult(results={})

    result = get_bulk_ont_status(db, onts, include_optical=True, mode=mode)

    # Apply results to models
    for ont in onts:
        ont_result = result.results.get(str(ont.id))
        if ont_result and ont_result.success:
            _apply_status_to_ont(ont, ont_result)

    return result


def _apply_status_to_ont(ont: OntUnit, result: OntStatusResult) -> None:
    """Apply status result to ONT model fields."""
    from app.services.network.ont_status import apply_status_snapshot, OntStatusSnapshot

    snapshot = OntStatusSnapshot(
        olt_status=result.online_status,
        acs_status=result.acs_status,
        acs_last_inform_at=result.acs_last_inform_at,
        effective_status=result.online_status,
        effective_status_source=result.status_source,
        status_resolved_at=result.resolved_at,
    )
    apply_status_snapshot(ont, snapshot)

    if result.optical_metrics and result.optical_metrics.has_signal_data:
        metrics = result.optical_metrics
        if metrics.olt_rx_dbm is not None:
            ont.olt_rx_signal_dbm = metrics.olt_rx_dbm
        if metrics.onu_rx_dbm is not None:
            ont.onu_rx_signal_dbm = metrics.onu_rx_dbm
        if metrics.onu_tx_dbm is not None:
            ont.onu_tx_signal_dbm = metrics.onu_tx_dbm
        if metrics.temperature_c is not None:
            ont.temperature_c = metrics.temperature_c
        if metrics.voltage_v is not None:
            ont.voltage_v = metrics.voltage_v
        if metrics.bias_current_ma is not None:
            ont.bias_current_ma = metrics.bias_current_ma
        if metrics.distance_m is not None:
            ont.distance_meters = metrics.distance_m
