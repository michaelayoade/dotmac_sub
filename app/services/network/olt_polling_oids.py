"""SNMP OID tables and scale factors for per-vendor ONT optical signal polling."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# SNMP OID tables for per-vendor ONT optical signal polling
# ---------------------------------------------------------------------------
# Each vendor uses different OID subtrees to expose per-ONU signal data.
# The OIDs below are walked from the OLT and return per-ONU index values.

_VENDOR_OID_OIDS: dict[str, dict[str, str]] = {
    "huawei": {
        # hwGponOltOpticsDdmInfoRxPower — OLT receive power per ONU
        "olt_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        # hwGponOltOpticsDdmInfoTxPower — ONU receive (reported via OLT)
        "onu_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
        # hwGponOntOpticalDdmTxPower — ONU transmit power
        "onu_tx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.2",
        # hwGponOntOpticalDdmTemperature — ONU laser temperature
        "temperature": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.3",
        # hwGponOntOpticalDdmBiasCurrent — ONU laser bias current
        "bias_current": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.5",
        # hwGponOntOpticalDdmVoltage — ONU supply voltage
        "voltage": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.7",
        # hwGponOltEponOnuDistance
        "distance": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
        # hwGponDeviceOnuRunStatus
        "status": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
        # hwGponDeviceOntLastDownCause
        "offline_reason": ".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.12",
        # hwGponDeviceOntSN
        "serial_number": ".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.2",
    },
    "zte": {
        "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
        "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
        "onu_tx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.4",
        "temperature": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.5",
        "bias_current": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.8",
        "voltage": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.6",
        "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
        "offline_reason": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.11",
        "serial_number": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.3",
    },
    "nokia": {
        "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
        "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
        "onu_tx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.3",
        "temperature": ".1.3.6.1.4.1.637.61.1.35.10.14.1.5",
        "bias_current": ".1.3.6.1.4.1.637.61.1.35.10.14.1.7",
        "voltage": ".1.3.6.1.4.1.637.61.1.35.10.14.1.6",
        "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
        "offline_reason": ".1.3.6.1.4.1.637.61.1.35.10.1.1.10",
        "serial_number": ".1.3.6.1.4.1.637.61.1.35.10.1.1.3",
    },
}

# Signal values are in 0.01 dBm units for most vendors
_VENDOR_SIGNAL_SCALE: dict[str, float] = {
    "huawei": 0.01,
    "zte": 0.01,
    "nokia": 0.01,
}

# DDM value scale factors per vendor.
# Temperature: 0.1°C units for Huawei, 1.0 for others.
# Voltage: 0.01V units for Huawei/ZTE, 0.001V for Nokia.
# Bias current: 0.001 mA for Huawei, 0.002 mA for ZTE, 0.001 for Nokia.
_VENDOR_DDM_SCALES: dict[str, dict[str, float]] = {
    "huawei": {"temperature": 0.1, "voltage": 0.01, "bias_current": 0.001},
    "zte": {"temperature": 1.0, "voltage": 0.01, "bias_current": 0.002},
    "nokia": {"temperature": 1.0, "voltage": 0.001, "bias_current": 0.001},
}

_DEFAULT_DDM_SCALES: dict[str, float] = {
    "temperature": 1.0,
    "voltage": 0.01,
    "bias_current": 0.001,
}


def _get_ddm_scales(vendor: str) -> dict[str, float]:
    """Return DDM value scale factors for a vendor."""
    vendor_lower = vendor.lower().strip()
    for key, scales in _VENDOR_DDM_SCALES.items():
        if key in vendor_lower:
            return scales
    return _DEFAULT_DDM_SCALES


# Sentinel values commonly used by vendors to indicate invalid/unavailable optics.
_SIGNAL_SENTINELS: set[int] = {
    2147483647,
    2147483646,
    65535,
    65534,
    32767,
    -2147483648,
}

# Generic/fallback OIDs (ITU-T G.988 standard GPON MIB)
GENERIC_OIDS: dict[str, str] = {
    "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
    "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
    "onu_tx": ".1.3.6.1.4.1.17409.2.3.6.10.1.4",
    "temperature": ".1.3.6.1.4.1.17409.2.3.6.10.1.5",
    "bias_current": ".1.3.6.1.4.1.17409.2.3.6.10.1.7",
    "voltage": ".1.3.6.1.4.1.17409.2.3.6.10.1.6",
    "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
    "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
}


def _resolve_oid_set(vendor: str) -> dict[str, str]:
    """Return the OID set for a given vendor, or generic fallback."""
    vendor_lower = vendor.lower().strip()
    for key, oids in _VENDOR_OID_OIDS.items():
        if key in vendor_lower:
            return oids
    return GENERIC_OIDS


def _get_signal_scale(vendor: str) -> float:
    """Return the signal value scale factor for a vendor."""
    vendor_lower = vendor.lower().strip()
    for key, scale in _VENDOR_SIGNAL_SCALE.items():
        if key in vendor_lower:
            return scale
    return 0.01
