"""Shared Huawei OLT SNMP profile helpers.

This module now delegates to olt_vendor_adapters.py for Huawei-specific
SNMP operations. The functions here provide backward compatibility.

For new code, use:
    from app.services.network.olt_vendor_adapters import get_olt_adapter
    adapter = get_olt_adapter(vendor="huawei", model="MA5800")
    fsp = adapter.decode_snmp_index(packed_value)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.network.olt_vendor_adapters import HuaweiOltAdapter, get_olt_adapter


@dataclass(frozen=True)
class HuaweiSnmpProfile:
    """Legacy profile class for backward compatibility."""

    key: str
    model_tokens: tuple[str, ...]
    oids: dict[str, str]
    packed_ports_per_slot: int = 32


# Get shared OIDs from adapter
_huawei_adapter = HuaweiOltAdapter()
_HUAWEI_SHARED_OIDS: dict[str, str] = _huawei_adapter.get_oid_set().to_dict()


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
    """Check if vendor string indicates Huawei."""
    return "huawei" in str(vendor or "").strip().lower()


def resolve_huawei_snmp_profile(model: str | None) -> HuaweiSnmpProfile:
    """Resolve SNMP profile for a Huawei model.

    Deprecated: Use get_olt_adapter(vendor="huawei", model=model)
    """
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
    """Best-effort decode of Huawei packed FSP index to frame/slot/port.

    Deprecated: Use get_olt_adapter(vendor="huawei").decode_snmp_index(packed_value)
    """
    adapter = get_olt_adapter(vendor="huawei", model=model)
    return adapter.decode_snmp_index(packed_value)
