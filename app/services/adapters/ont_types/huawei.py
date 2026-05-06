"""Huawei ONT type adapters - transforms only.

These adapters provide device-specific value transformations
(e.g., security mode mappings for TR-098 BeaconType).

TR-069 parameter paths are stored in the OnuType database table.
"""

from app.services.adapters.ont_types.base import OntTypeAdapter, ont_type_registry

# =============================================================================
# Huawei TR-098 Security Mode Mapping
# =============================================================================
# TR-098 uses BeaconType values: "None", "WPA", "11i", "WPAand11i"
# Standard modes map to these values.

HUAWEI_TR098_SECURITY_MAP = {
    # WPA2 variants -> 11i
    "WPA2": "11i",
    "WPA2-Personal": "11i",
    "WPA2-PSK": "11i",
    # WPA variants -> WPA
    "WPA": "WPA",
    "WPA-Personal": "WPA",
    "WPA-PSK": "WPA",
    # Mixed mode -> WPAand11i
    "WPA+WPA2": "WPAand11i",
    "WPA-WPA2-Personal": "WPAand11i",
    "Mixed": "WPAand11i",
    # Open/None -> None
    "None": "None",
    "Open": "None",
}

# WPA3 support for newer models
HUAWEI_WPA3_SECURITY_MAP = {
    **HUAWEI_TR098_SECURITY_MAP,
    "WPA3": "WPA3-SAE",
    "WPA3-Personal": "WPA3-SAE",
    "WPA2+WPA3": "11iandWPA3",
}


# =============================================================================
# Huawei Adapters
# =============================================================================

huawei_hg8245h = OntTypeAdapter(
    name="huawei-hg8245h",
    vendor="Huawei",
    security_mode_map=HUAWEI_TR098_SECURITY_MAP,
    notes="Huawei HG8245H/H5 - 4xGE, dual-band WiFi, 2xPOTS. TR-098.",
)
ont_type_registry.register(huawei_hg8245h)


huawei_hg8546m = OntTypeAdapter(
    name="huawei-hg8546m",
    vendor="Huawei",
    security_mode_map=HUAWEI_TR098_SECURITY_MAP,
    notes="Huawei HG8546M - 4xGE, dual-band WiFi, 1xPOTS. TR-098.",
)
ont_type_registry.register(huawei_hg8546m)


huawei_eg8145v5 = OntTypeAdapter(
    name="huawei-eg8145v5",
    vendor="Huawei",
    security_mode_map=HUAWEI_WPA3_SECURITY_MAP,
    notes="Huawei EG8145V5 - WiFi 6, 4xGE, 2xPOTS. TR-098 with WPA3.",
)
ont_type_registry.register(huawei_eg8145v5)


# Generic Huawei TR-098 adapter for unspecified models
huawei_generic_tr098 = OntTypeAdapter(
    name="huawei-generic-tr098",
    vendor="Huawei",
    security_mode_map=HUAWEI_TR098_SECURITY_MAP,
    notes="Generic Huawei TR-098 adapter. Use for unspecified Huawei ONTs.",
)
ont_type_registry.register(huawei_generic_tr098)
