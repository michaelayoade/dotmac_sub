"""Huawei OLT type adapters.

Defines firmware-specific capabilities for Huawei OLT models.

Key findings:
- MA5608T V800R013*: Does NOT support ont internet-config, ont wan-config
- MA5608T V800R015*: Supports home-gateway-config, not wan/internet-config
- MA5608T V800R018+: Supports wan-config, internet-config, home-gateway-config
- MA5800 V100R019+: Does NOT support ont wifi-config (no OMCI WiFi)
"""

from app.services.adapters.olt_types.base import (
    OltCapabilities,
    OltTypeAdapter,
    WanProvisioningMode,
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
        wan_provisioning_mode=WanProvisioningMode.TR069_ONLY.value,
        supports_ont_internet_config=False,
        supports_ont_wan_config=False,
        supports_ont_home_gateway_config=False,
        command_profile_name="huawei-ma5608t-v800r013",
        requires_slow_send=True,
        supports_slash_fsp_display=False,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5608T V800R013 - limited OMCI. Use service-port + ipconfig + TR-069.",
)
olt_type_registry.register(ma5608t_v800r013)


# =============================================================================
# MA5608T V800R015 - Home-gateway-config only
# =============================================================================
# Karsana V800R015 supports ont home-gateway-config but not
# ont internet-config or ont wan-config.

ma5608t_v800r015 = OltTypeAdapter(
    name="huawei-ma5608t-v800r015",
    vendor="Huawei",
    model_patterns=["MA5608T"],
    firmware_patterns=[r"V800R015"],
    capabilities=OltCapabilities(
        wan_provisioning_mode=WanProvisioningMode.HOME_GATEWAY_CONFIG.value,
        supports_ont_internet_config=False,
        supports_ont_wan_config=False,
        supports_ont_home_gateway_config=True,
        command_profile_name="huawei-ma5608t-v800r015",
        requires_slow_send=True,
        supports_slash_fsp_display=False,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5608T V800R015 - use home-gateway-config; skip wan/internet-config.",
)
olt_type_registry.register(ma5608t_v800r015)


# =============================================================================
# MA5608T V800R018+ - Extended capabilities
# =============================================================================
# V800R018 is the first verified MA5608T train in this fleet with
# internet-config and wan-config support. Register after older specific
# versions so V800R013/V800R015 do not fall into the full-capability bucket.

ma5608t_v800r018 = OltTypeAdapter(
    name="huawei-ma5608t-v800r018",
    vendor="Huawei",
    model_patterns=["MA5608T"],
    firmware_patterns=[r"V800R018", r"V800R019", r"V800R02"],
    capabilities=OltCapabilities(
        wan_provisioning_mode=WanProvisioningMode.OMCI_WAN_CONFIG.value,
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_home_gateway_config=True,
        command_profile_name="huawei-ma5608t-v800r018",
        requires_slow_send=True,
        supports_slash_fsp_display=False,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=True,
        supports_traffic_table=True,
    ),
    notes="MA5608T V800R018+ - full verified OMCI WAN provisioning support.",
)
olt_type_registry.register(ma5608t_v800r018)


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
        wan_provisioning_mode=WanProvisioningMode.OMCI_WAN_CONFIG.value,
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_home_gateway_config=False,
        command_profile_name="huawei-ma5800-v800r019",
        requires_slow_send=False,
        supports_slash_fsp_display=True,
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
        wan_provisioning_mode=WanProvisioningMode.OMCI_WAN_CONFIG.value,
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
        supports_ont_home_gateway_config=False,
        command_profile_name="huawei-ma5800-v100r019",
        requires_slow_send=False,
        supports_slash_fsp_display=True,
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
        wan_provisioning_mode=WanProvisioningMode.TR069_ONLY.value,
        supports_ont_internet_config=False,
        supports_ont_wan_config=False,
        supports_ont_home_gateway_config=False,
        command_profile_name="huawei-generic",
        requires_slow_send=True,
        supports_slash_fsp_display=False,
        supports_ont_wifi_config=False,
        supports_ont_port_vlan=False,
        supports_traffic_table=False,
    ),
    notes="Generic Huawei OLT - read-only until its firmware profile is verified.",
)
olt_type_registry.register(huawei_generic)
