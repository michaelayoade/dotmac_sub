"""SNMP configuration resolution for OLT polling.

Provides per-vendor/model SNMP configuration from DB with fallback to
hardcoded defaults. Addresses issue #17 where bulkwalk strategy was
hardcoded and couldn't be customized per device.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, VendorSnmpConfig
from app.services.network.olt_polling_oids import (
    _VENDOR_OID_OIDS,
    _VENDOR_SIGNAL_SCALE,
)

logger = logging.getLogger(__name__)

# Default SNMP walk configuration
_DEFAULT_WALK_STRATEGY = "single"
_DEFAULT_WALK_TIMEOUT = 90
_DEFAULT_MAX_REPETITIONS = 50


def resolve_snmp_config(
    db: Session,
    vendor: str,
    model: str | None,
) -> dict[str, Any]:
    """Resolve SNMP config from DB with fallback to hardcoded defaults.

    Resolution order:
    1. Exact vendor+model match (highest priority)
    2. Vendor-only match (model is NULL)
    3. Hardcoded defaults

    Args:
        db: Database session
        vendor: OLT vendor name (e.g., "Huawei", "ZTE")
        model: OLT model name (e.g., "MA5608T"), or None

    Returns:
        Dict with keys: walk_strategy, timeout, max_repetitions, oids, signal_scale
    """
    vendor_lower = vendor.lower().strip() if vendor else ""

    if not vendor_lower:
        return _get_default_config("")

    config: VendorSnmpConfig | None = None

    try:
        # Try exact vendor+model match first
        if model:
            config = db.scalars(
                select(VendorSnmpConfig)
                .where(VendorSnmpConfig.vendor.ilike(f"%{vendor_lower}%"))
                .where(VendorSnmpConfig.model.ilike(f"%{model}%"))
                .where(VendorSnmpConfig.is_active.is_(True))
                .order_by(VendorSnmpConfig.priority.desc())
                .limit(1)
            ).first()

        # Fallback to vendor-only match
        if config is None:
            config = db.scalars(
                select(VendorSnmpConfig)
                .where(VendorSnmpConfig.vendor.ilike(f"%{vendor_lower}%"))
                .where(VendorSnmpConfig.model.is_(None))
                .where(VendorSnmpConfig.is_active.is_(True))
                .order_by(VendorSnmpConfig.priority.desc())
                .limit(1)
            ).first()

    except Exception as exc:
        logger.warning(
            "Failed to load SNMP config for vendor=%s model=%s: %s",
            vendor,
            model,
            exc,
        )
        return _get_default_config(vendor_lower)

    if config:
        logger.debug(
            "Using DB SNMP config for vendor=%s model=%s: strategy=%s",
            vendor,
            model,
            config.walk_strategy,
        )
        return {
            "walk_strategy": config.walk_strategy or _DEFAULT_WALK_STRATEGY,
            "timeout": config.walk_timeout_seconds or _DEFAULT_WALK_TIMEOUT,
            "max_repetitions": config.walk_max_repetitions or _DEFAULT_MAX_REPETITIONS,
            "oids": _merge_oid_overrides(vendor_lower, config.oid_overrides),
            "signal_scale": config.signal_scale or _get_default_signal_scale(vendor_lower),
        }

    return _get_default_config(vendor_lower)


def resolve_snmp_config_for_olt(
    db: Session,
    olt: OLTDevice,
) -> dict[str, Any]:
    """Convenience function to resolve SNMP config for an OLT device."""
    return resolve_snmp_config(db, olt.vendor or "", olt.model)


def _get_default_config(vendor_lower: str) -> dict[str, Any]:
    """Get hardcoded default SNMP config for a vendor."""
    return {
        "walk_strategy": _DEFAULT_WALK_STRATEGY,
        "timeout": _DEFAULT_WALK_TIMEOUT,
        "max_repetitions": _DEFAULT_MAX_REPETITIONS,
        "oids": _get_default_oids(vendor_lower),
        "signal_scale": _get_default_signal_scale(vendor_lower),
    }


def _get_default_oids(vendor_lower: str) -> dict[str, str]:
    """Get default OIDs for a vendor."""
    for key, oids in _VENDOR_OID_OIDS.items():
        if key in vendor_lower:
            return dict(oids)
    return {}


def _get_default_signal_scale(vendor_lower: str) -> float:
    """Get default signal scale factor for a vendor."""
    for key, scale in _VENDOR_SIGNAL_SCALE.items():
        if key in vendor_lower:
            return scale
    return 0.01  # Most common default


def _merge_oid_overrides(
    vendor_lower: str,
    overrides: dict | None,
) -> dict[str, str]:
    """Merge OID overrides with defaults."""
    base_oids = _get_default_oids(vendor_lower)
    if overrides:
        base_oids.update(overrides)
    return base_oids
