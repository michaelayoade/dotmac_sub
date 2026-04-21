"""ONT signal quality thresholds and classification.

Provides functions to load configurable signal thresholds and classify
ONT optical signal quality.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

# Default thresholds — overridden by settings at runtime
DEFAULT_WARN_THRESHOLD = -25.0  # dBm
DEFAULT_CRIT_THRESHOLD = -28.0  # dBm

# Signal quality classifications
SIGNAL_QUALITY_GOOD = "good"
SIGNAL_QUALITY_WARNING = "warning"
SIGNAL_QUALITY_CRITICAL = "critical"
SIGNAL_QUALITY_UNKNOWN = "unknown"

OPTICAL_SIGNAL_MIN_DBM = -50.0
OPTICAL_SIGNAL_MAX_DBM = 10.0


def normalize_optical_signal_dbm(dbm: float | None) -> float | None:
    """Return a sane optical signal value or ``None``.

    Some legacy ingest paths persisted sentinel or overflow values such as
    ``21474836.47`` into the DB. Treat anything outside the physically
    plausible range as missing data.
    """
    if dbm is None:
        return None
    try:
        value = float(dbm)
    except (TypeError, ValueError):
        return None
    if value < OPTICAL_SIGNAL_MIN_DBM or value > OPTICAL_SIGNAL_MAX_DBM:
        return None
    return value


def classify_signal(
    dbm: float | None,
    *,
    warn_threshold: float = DEFAULT_WARN_THRESHOLD,
    crit_threshold: float = DEFAULT_CRIT_THRESHOLD,
) -> str:
    """Classify optical signal quality based on dBm value.

    Args:
        dbm: Signal level in dBm (negative values)
        warn_threshold: Warning threshold (e.g., -25.0)
        crit_threshold: Critical threshold (e.g., -28.0)

    Returns:
        One of: "good", "warning", "critical", "unknown"
    """
    dbm = normalize_optical_signal_dbm(dbm)
    if dbm is None:
        return SIGNAL_QUALITY_UNKNOWN
    if dbm < crit_threshold:
        return SIGNAL_QUALITY_CRITICAL
    if dbm < warn_threshold:
        return SIGNAL_QUALITY_WARNING
    return SIGNAL_QUALITY_GOOD


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

    Returns:
        Tuple of (warning_threshold, critical_threshold) in dBm
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
    from app.models.network import SignalThresholdOverride

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

        return None
    except Exception as exc:
        logger.warning("Failed to check threshold override: %s", exc)
        return None
