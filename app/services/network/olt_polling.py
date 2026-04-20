"""OLT optical signal utilities.

Provides signal classification, threshold management, and SNMP status
reconciliation for OLT/ONT monitoring. Actual SNMP polling has been
moved to Zabbix; this module retains utilities used by web UI and
other monitoring services.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OnuOfflineReason,
    OnuOnlineStatus,
    SignalThresholdOverride,
)

# Re-exports for backwards compatibility with importers
from app.services.network.olt_polling_metrics import (
    _push_olt_health_metrics as _push_olt_health_metrics,  # noqa: F401
)
from app.services.network.olt_polling_metrics import (
    push_signal_metrics_to_victoriametrics as push_signal_metrics_to_victoriametrics,  # noqa: F401
)
from app.services.network.olt_polling_oids import (
    _SIGNAL_SENTINELS as _SIGNAL_SENTINELS,  # noqa: F401
)
from app.services.network.olt_polling_oids import (
    _VENDOR_OID_OIDS as _VENDOR_OID_OIDS,  # noqa: F401
)
from app.services.network.olt_polling_oids import (
    GENERIC_OIDS as GENERIC_OIDS,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    OltHealthReading as OltHealthReading,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    OntSignalReading as OntSignalReading,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _derive_offline_reason as _derive_offline_reason,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _parse_ddm_value as _parse_ddm_value,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _parse_online_status as _parse_online_status,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _parse_signal_value as _parse_signal_value,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _parse_snmp_table as _parse_snmp_table,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _run_olt_snmpwalk as _run_olt_snmpwalk,  # noqa: F401
)
from app.services.network.olt_polling_parsers import (
    _split_onu_index as _split_onu_index,  # noqa: F401
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal threshold classification
# ---------------------------------------------------------------------------

# Default thresholds — overridden by settings at runtime
DEFAULT_WARN_THRESHOLD = -25.0  # dBm
DEFAULT_CRIT_THRESHOLD = -28.0  # dBm

SIGNAL_QUALITY_GOOD = "good"
SIGNAL_QUALITY_WARNING = "warning"
SIGNAL_QUALITY_CRITICAL = "critical"
SIGNAL_QUALITY_UNKNOWN = "unknown"


def classify_signal(
    dbm: float | None,
    *,
    warn_threshold: float = DEFAULT_WARN_THRESHOLD,
    crit_threshold: float = DEFAULT_CRIT_THRESHOLD,
) -> str:
    """Classify optical signal quality based on dBm value.

    Args:
        dbm: Optical power in dBm (negative values).
        warn_threshold: dBm value below which signal is 'warning'.
        crit_threshold: dBm value below which signal is 'critical'.

    Returns:
        One of: 'good', 'warning', 'critical', 'unknown'.
    """
    if dbm is None:
        return SIGNAL_QUALITY_UNKNOWN
    if dbm >= warn_threshold:
        return SIGNAL_QUALITY_GOOD
    if dbm >= crit_threshold:
        return SIGNAL_QUALITY_WARNING
    return SIGNAL_QUALITY_CRITICAL


def get_signal_thresholds(
    db: Session,
    *,
    olt: OLTDevice | None = None,
) -> tuple[float, float]:
    """Load signal thresholds with per-OLT/model override support.

    Resolution order:
    1. Per-OLT override (if olt is provided and has an override)
    2. Per-model override (if olt is provided and model has an override)
    3. Global settings
    4. Hardcoded defaults

    Args:
        db: Database session
        olt: Optional OLT device for per-device override lookup
    """
    # Check for per-OLT or per-model overrides if OLT is provided
    if olt:
        override_thresholds = _get_threshold_override(db, olt)
        if override_thresholds:
            return override_thresholds

    # Fall back to global settings
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        warn_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_warning_dbm"
        )
        crit_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_critical_dbm"
        )
        warn = float(str(warn_raw)) if warn_raw is not None else DEFAULT_WARN_THRESHOLD
        crit = float(str(crit_raw)) if crit_raw is not None else DEFAULT_CRIT_THRESHOLD
        return warn, crit
    except Exception as exc:
        logger.warning("Failed to load signal thresholds, using defaults: %s", exc)
        return DEFAULT_WARN_THRESHOLD, DEFAULT_CRIT_THRESHOLD


def _get_threshold_override(
    db: Session,
    olt: OLTDevice,
) -> tuple[float, float] | None:
    """Check for per-OLT or per-model threshold overrides.

    Returns:
        Tuple of (warning, critical) thresholds, or None if no override found.
    """
    try:
        # 1. Check for per-OLT override
        override = db.scalars(
            select(SignalThresholdOverride)
            .where(SignalThresholdOverride.olt_device_id == olt.id)
            .where(SignalThresholdOverride.is_active.is_(True))
        ).first()

        if override and override.warning_threshold_dbm is not None:
            logger.debug(
                "Using per-OLT threshold override for %s: warn=%.1f crit=%.1f",
                olt.name,
                override.warning_threshold_dbm,
                override.critical_threshold_dbm or DEFAULT_CRIT_THRESHOLD,
            )
            return (
                override.warning_threshold_dbm,
                override.critical_threshold_dbm or DEFAULT_CRIT_THRESHOLD,
            )

        # 2. Check for per-model override
        if olt.model:
            override = db.scalars(
                select(SignalThresholdOverride)
                .where(SignalThresholdOverride.model_pattern.ilike(f"%{olt.model}%"))
                .where(SignalThresholdOverride.olt_device_id.is_(None))
                .where(SignalThresholdOverride.is_active.is_(True))
            ).first()

            if override and override.warning_threshold_dbm is not None:
                logger.debug(
                    "Using per-model threshold override for %s (model=%s): warn=%.1f crit=%.1f",
                    olt.name,
                    olt.model,
                    override.warning_threshold_dbm,
                    override.critical_threshold_dbm or DEFAULT_CRIT_THRESHOLD,
                )
                return (
                    override.warning_threshold_dbm,
                    override.critical_threshold_dbm or DEFAULT_CRIT_THRESHOLD,
                )

    except Exception as exc:
        logger.warning(
            "Failed to check threshold overrides for OLT %s: %s",
            olt.name,
            exc,
        )

    return None


# ---------------------------------------------------------------------------
# SNMP status reconciliation
# ---------------------------------------------------------------------------


def reconcile_snmp_status_with_signal(
    *,
    vendor: str,
    raw_status: str | None,
    olt_rx_dbm: float | None,
) -> tuple[OnuOnlineStatus, OnuOfflineReason | None, bool]:
    """Determine effective ONT status using SNMP state and signal level.

    Args:
        vendor: OLT vendor key (e.g. "huawei", "zte").
        raw_status: Raw SNMP online/offline state string.
        olt_rx_dbm: OLT-side received signal in dBm.

    Returns:
        Tuple of (status, offline_reason, was_reconciled).
    """
    import re

    if raw_status is None:
        return OnuOnlineStatus.unknown, None, False

    state_lower = raw_status.lower().strip()

    def _has_valid_signal() -> bool:
        return olt_rx_dbm is not None and -30.0 < olt_rx_dbm < 0.0

    numeric = re.search(r"(-?\d+)", state_lower)
    if numeric:
        code = int(numeric.group(1))
        if code == 1:
            return OnuOnlineStatus.online, None, False
        if code in {2, 3, 4, 5}:
            if _has_valid_signal():
                logger.warning(
                    "SNMP reports offline code %s but has valid signal %.1f dBm (vendor=%s)",
                    code,
                    olt_rx_dbm,
                    vendor,
                )
                return OnuOnlineStatus.online, None, True
            reason = _derive_offline_reason(state_lower)
            try:
                return (
                    OnuOnlineStatus.offline,
                    OnuOfflineReason(reason) if reason else OnuOfflineReason.unknown,
                    False,
                )
            except ValueError:
                return OnuOnlineStatus.offline, OnuOfflineReason.unknown, False

    # Map SNMP states to status
    if state_lower in ("online", "up", "1", "active"):
        return OnuOnlineStatus.online, None, False

    if state_lower in ("offline", "down", "0", "inactive", "2"):
        # If SNMP says offline but we have a valid signal, something is off
        if _has_valid_signal():
            logger.warning(
                "SNMP reports offline but has valid signal %.1f dBm (vendor=%s)",
                olt_rx_dbm,
                vendor,
            )
            # Reconcile: trust signal over SNMP state
            return OnuOnlineStatus.online, None, True
        return OnuOnlineStatus.offline, OnuOfflineReason.unknown, False

    # Handle vendor-specific offline reasons
    if "los" in state_lower or "loss" in state_lower:
        return OnuOnlineStatus.offline, OnuOfflineReason.los, False
    if "dying" in state_lower:
        return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp, False

    return OnuOnlineStatus.unknown, None, False
