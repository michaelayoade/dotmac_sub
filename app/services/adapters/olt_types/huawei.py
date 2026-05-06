"""Huawei OLT type adapters.

Defines firmware-specific capabilities for Huawei OLT models.

Key findings:
- MA5608T V800R013*: Does NOT support ont internet-config, ont wan-config
- MA5800 V800R019+: Supports all commands
- MA5800 V100R019+: Does NOT support ont wifi-config (no OMCI WiFi)
"""

from app.services.adapters.olt_types.base import (
    OltCapabilities,
    OltTypeAdapter,
    olt_type_registry,
)

# =============================================================================
# MA5608T V800R013 - Limited capabilities
# =============================================================================
# This older firmware does NOT support:
# - ont internet-config (TCP stack activation)
# - ont wan-config (route+NAT mode)
#
# Working path: service-port + ont ipconfig + TR-069 profile bind + ACS

ma5608t_v800r013 = OltTypeAdapter(
    name="huawei-ma5608t-v800r013",
    vendor="Huawei",
    model_patterns=["MA5608T"],
    firmware_patterns=[r"V800R013"],
    capabilities=OltCapabilities(
        supports_ont_internet_config=False,
        supports_ont_wan_config=False,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5608T V800R013 - limited OMCI. Use service-port + ipconfig + TR-069.",
)
olt_type_registry.register(ma5608t_v800r013)


# =============================================================================
# MA5608T V800R017+ - Extended capabilities
# =============================================================================
# Newer MA5608T firmware may support internet-config/wan-config.
# Register this AFTER v800r013 so specific version matches first.

ma5608t_v800r017 = OltTypeAdapter(
    name="huawei-ma5608t-v800r017",
    vendor="Huawei",
    model_patterns=["MA5608T"],
    firmware_patterns=[r"V800R017", r"V800R018", r"V800R019", r"V800R02"],
    capabilities=OltCapabilities(
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5608T V800R017+ - full OMCI provisioning support.",
)
olt_type_registry.register(ma5608t_v800r017)


# =============================================================================
# MA5800 V800R019+ - Full capabilities
# =============================================================================
# The MA5800 series with V800R019+ firmware supports all OMCI commands
# except WiFi config (which requires V100R019+ and specific ONT models).

ma5800_v800r019 = OltTypeAdapter(
    name="huawei-ma5800-v800r019",
    vendor="Huawei",
    model_patterns=["MA5800"],
    firmware_patterns=[r"V800R019", r"V800R02", r"V800R03"],
    capabilities=OltCapabilities(
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_wifi_config=False,  # Requires V100R019+
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5800 V800R019+ - full OMCI provisioning support (no WiFi OMCI).",
)
olt_type_registry.register(ma5800_v800r019)


# =============================================================================
# MA5800 V100R019+ - WiFi OMCI support
# =============================================================================
# V100R019 adds ont wifi-config command for OMCI-based WiFi provisioning.
# Note: Per testing, this command wasn't found on V100R019 - may need
# specific board/ONT support. Keeping flag False until verified.

ma5800_v100r019 = OltTypeAdapter(
    name="huawei-ma5800-v100r019",
    vendor="Huawei",
    model_patterns=["MA5800"],
    firmware_patterns=[r"V100R019", r"V100R02"],
    capabilities=OltCapabilities(
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_wifi_config=False,  # Command exists but needs ONT support
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5800 V100R019+ - ont wifi-config exists but ONT support varies.",
)
olt_type_registry.register(ma5800_v100r019)


# =============================================================================
# Generic Huawei fallback - conservative defaults
# =============================================================================
# For unrecognized Huawei OLTs, assume limited capabilities to be safe.

huawei_generic = OltTypeAdapter(
    name="huawei-generic",
    vendor="Huawei",
    model_patterns=["MA5"],  # Matches any MA5xxx
    firmware_patterns=[],  # Any firmware
    capabilities=OltCapabilities(
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="Generic Huawei OLT - assumes standard capabilities.",
)
olt_type_registry.register(huawei_generic)
