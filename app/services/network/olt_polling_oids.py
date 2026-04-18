"""SNMP OID tables and scale factors for per-vendor ONT optical signal polling.

This module now delegates to olt_vendor_adapters.py for vendor-specific
OID sets and scale factors. The functions here provide backward compatibility
for existing code that imports from this module.

For new code, use:
    from app.services.network.olt_vendor_adapters import get_olt_adapter
    adapter = get_olt_adapter(vendor="huawei")
    oids = adapter.get_oid_set()
"""

from __future__ import annotations

from app.services.network.olt_vendor_adapters import (
    SIGNAL_SENTINELS as _SIGNAL_SENTINELS,
    GenericOltAdapter,
    HuaweiOltAdapter,
    NokiaOltAdapter,
    ZteOltAdapter,
    get_olt_adapter,
)

# Re-export sentinel values for backward compatibility
_SIGNAL_SENTINELS = _SIGNAL_SENTINELS

# Backward compatibility: vendor OID dict (used by olt_polling.py)
_VENDOR_OID_OIDS: dict[str, dict[str, str]] = {
    "huawei": HuaweiOltAdapter().get_oid_set().to_dict(),
    "zte": ZteOltAdapter().get_oid_set().to_dict(),
    "nokia": NokiaOltAdapter().get_oid_set().to_dict(),
}

# Generic/fallback OIDs (ITU-T G.988 standard GPON MIB) - for backward compatibility
GENERIC_OIDS: dict[str, str] = GenericOltAdapter().get_oid_set().to_dict()


def _get_ddm_scales(vendor: str) -> dict[str, float]:
    """Return DDM value scale factors for a vendor.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_ddm_scales()
    """
    adapter = get_olt_adapter(vendor=vendor)
    scales = adapter.get_ddm_scales()
    return {
        "temperature": scales.temperature_c,
        "voltage": scales.voltage_v,
        "bias_current": scales.bias_current_ma,
    }


def _resolve_oid_set(vendor: str) -> dict[str, str]:
    """Return the OID set for a given vendor, or generic fallback.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_oid_set().to_dict()
    """
    return get_olt_adapter(vendor=vendor).get_oid_set().to_dict()


def _get_signal_scale(vendor: str) -> float:
    """Return the signal value scale factor for a vendor.

    Deprecated: Use get_olt_adapter(vendor=vendor).get_signal_scale()
    """
    return get_olt_adapter(vendor=vendor).get_signal_scale()
