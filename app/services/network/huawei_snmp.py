"""Shared Huawei OLT SNMP profile helpers.

Huawei OLT families share most GPON OIDs, but model-specific handling still
matters for index decoding and future overrides. Centralizing the profile
definition keeps pollers, sync jobs, and UI helpers aligned.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HuaweiSnmpProfile:
    key: str
    model_tokens: tuple[str, ...]
    oids: dict[str, str]
    packed_ports_per_slot: int = 32


_HUAWEI_SHARED_OIDS: dict[str, str] = {
    # hwGponOltOpticsDdmInfoRxPower
    "olt_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
    # hwGponOltOpticsDdmInfoTxPower
    "onu_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
    # hwGponDeviceOntControlDistance
    "distance": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
    # hwGponDeviceOntControlRunStatus
    "status": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
    # hwGponDeviceOntControlLastDownCause
    "last_down_cause": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.24",
}


_PROFILES: tuple[HuaweiSnmpProfile, ...] = (
    HuaweiSnmpProfile(
        key="huawei_ma5600",
        model_tokens=("ma5600",),
        oids=dict(_HUAWEI_SHARED_OIDS),
    ),
    HuaweiSnmpProfile(
        key="huawei_ma5608t",
        model_tokens=("ma5608t",),
        oids=dict(_HUAWEI_SHARED_OIDS),
    ),
    HuaweiSnmpProfile(
        key="huawei_ma5800",
        model_tokens=("ma5800", "ma5800-x2"),
        oids=dict(_HUAWEI_SHARED_OIDS),
    ),
)

_DEFAULT_PROFILE = HuaweiSnmpProfile(
    key="huawei_generic",
    model_tokens=(),
    oids=dict(_HUAWEI_SHARED_OIDS),
)


def is_huawei_vendor(vendor: str | None) -> bool:
    return "huawei" in str(vendor or "").strip().lower()


def resolve_huawei_snmp_profile(model: str | None) -> HuaweiSnmpProfile:
    model_text = str(model or "").strip().lower()
    for profile in _PROFILES:
        if any(token in model_text for token in profile.model_tokens):
            return profile
    return _DEFAULT_PROFILE


def decode_huawei_packed_fsp(
    packed_value: int,
    *,
    model: str | None = None,
) -> str | None:
    """Best-effort decode of Huawei packed FSP index to frame/slot/port."""
    if packed_value < 0:
        return None
    base = 0xFA000000
    if packed_value < base:
        return None
    delta = packed_value - base
    if delta % 256 != 0:
        return None
    profile = resolve_huawei_snmp_profile(model)
    slot_port = delta // 256
    frame = 0
    slot = slot_port // profile.packed_ports_per_slot
    port = slot_port % profile.packed_ports_per_slot
    if slot < 0 or port < 0:
        return None
    return f"{frame}/{slot}/{port}"
