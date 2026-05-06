"""OLT Config Pack resolver for centralized ONT provisioning defaults.

The OLT Config Pack provides a single source of truth for all default
configuration values that ONTs inherit from their parent OLT. This includes:

- Authorization profile IDs (line/service profiles)
- TR-069 binding profile ID
- VLAN assignments by purpose (internet, management, TR-069)
- Provisioning knobs (ip-index, wan-config profile)
- Connection request credentials

Usage:
    from app.services.network.olt_config_pack import resolve_olt_config_pack

    config = resolve_olt_config_pack(db, olt_id)
    # Use config.line_profile_id, config.internet_vlan_tag, etc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from uuid import UUID

    from app.models.network import OLTDevice, Vlan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VlanConfig:
    """Resolved VLAN configuration with both ID and tag."""

    id: str | None = None
    tag: int | None = None
    name: str | None = None
    purpose: str | None = None

    @classmethod
    def from_vlan(cls, vlan: Vlan | None) -> VlanConfig:
        if vlan is None:
            return cls()
        return cls(
            id=str(vlan.id) if vlan.id else None,
            tag=vlan.tag,
            name=vlan.name,
            purpose=vlan.purpose.value if vlan.purpose else None,
        )


@dataclass(frozen=True)
class OltConfigPack:
    """Complete OLT configuration pack for ONT provisioning.

    All values are resolved and ready to use. None values indicate
    the setting is not configured at the OLT level.
    """

    # OLT identity
    olt_id: str
    olt_name: str

    # Authorization profiles (OLT-local IDs)
    line_profile_id: int | None = None
    service_profile_id: int | None = None

    # TR-069 configuration
    tr069_acs_server_id: str | None = None
    tr069_olt_profile_id: int | None = None

    # VLANs by purpose (resolved with tags)
    internet_vlan: VlanConfig = field(default_factory=VlanConfig)
    management_vlan: VlanConfig = field(default_factory=VlanConfig)
    tr069_vlan: VlanConfig = field(default_factory=VlanConfig)
    voip_vlan: VlanConfig = field(default_factory=VlanConfig)
    iptv_vlan: VlanConfig = field(default_factory=VlanConfig)

    # OLT-side provisioning knobs
    # None = skip command (e.g., OLT firmware doesn't support it)
    # 0+ = valid ip-index for ont internet-config command
    internet_config_ip_index: int | None = None
    wan_config_profile_id: int | None = None  # None = skip wan-config; 0+ = valid profile
    wan_provisioning_mode: str = "omci_wan_config"
    supports_ont_home_gateway_config: bool = False

    # GEM port indices by purpose
    internet_gem_index: int | None = None
    mgmt_gem_index: int | None = None
    voip_gem_index: int | None = None
    iptv_gem_index: int | None = None

    # TR-069 connection request credentials
    cr_username: str | None = None
    cr_password: str | None = None

    # Traffic table indices for service-port QoS binding
    mgmt_traffic_table_inbound: int | None = None
    mgmt_traffic_table_outbound: int | None = None
    internet_traffic_table_inbound: int | None = None
    internet_traffic_table_outbound: int | None = None

    # TR-069 WAN Connection Device indices (OLT-provisioning-specific)
    # Mapping: OLT ip-index N → TR-069 WANConnectionDevice.(N+1)
    pppoe_wcd_index: int | None = None
    mgmt_wcd_index: int | None = None
    voip_wcd_index: int | None = None  # VoIP WCD if provisioned

    @property
    def has_authorization_profiles(self) -> bool:
        """True if both line and service profiles are configured."""
        return self.line_profile_id is not None and self.service_profile_id is not None

    @property
    def has_tr069_config(self) -> bool:
        """True if TR-069 ACS and OLT profile are configured."""
        return (
            self.tr069_acs_server_id is not None
            and self.tr069_olt_profile_id is not None
        )

    @property
    def has_vlans(self) -> bool:
        """True if at least internet and management VLANs are configured."""
        return (
            self.internet_vlan.tag is not None
            and self.management_vlan.tag is not None
        )

    @property
    def is_complete(self) -> bool:
        """True if all essential config pack fields are populated."""
        return (
            self.has_authorization_profiles
            and self.has_vlans
            and self.has_tr069_config
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "line_profile_id": self.line_profile_id,
            "service_profile_id": self.service_profile_id,
            "tr069_acs_server_id": self.tr069_acs_server_id,
            "tr069_olt_profile_id": self.tr069_olt_profile_id,
            "internet_vlan": {
                "id": self.internet_vlan.id,
                "tag": self.internet_vlan.tag,
                "name": self.internet_vlan.name,
            },
            "management_vlan": {
                "id": self.management_vlan.id,
                "tag": self.management_vlan.tag,
                "name": self.management_vlan.name,
            },
            "tr069_vlan": {
                "id": self.tr069_vlan.id,
                "tag": self.tr069_vlan.tag,
                "name": self.tr069_vlan.name,
            },
            "internet_config_ip_index": self.internet_config_ip_index,
            "wan_config_profile_id": self.wan_config_profile_id,
            "wan_provisioning_mode": self.wan_provisioning_mode,
            "supports_ont_home_gateway_config": self.supports_ont_home_gateway_config,
            "internet_gem_index": self.internet_gem_index,
            "mgmt_gem_index": self.mgmt_gem_index,
            "voip_gem_index": self.voip_gem_index,
            "iptv_gem_index": self.iptv_gem_index,
            "cr_username": self.cr_username,
            "mgmt_traffic_table_inbound": self.mgmt_traffic_table_inbound,
            "mgmt_traffic_table_outbound": self.mgmt_traffic_table_outbound,
            "internet_traffic_table_inbound": self.internet_traffic_table_inbound,
            "internet_traffic_table_outbound": self.internet_traffic_table_outbound,
            "pppoe_wcd_index": self.pppoe_wcd_index,
            "mgmt_wcd_index": self.mgmt_wcd_index,
            "voip_wcd_index": self.voip_wcd_index,
            "is_complete": self.is_complete,
        }


def _resolve_vlan(db: Session, vlan_id: str | None) -> VlanConfig:
    """Resolve VLAN UUID to VlanConfig."""
    if not vlan_id:
        return VlanConfig()
    from app.models.network import Vlan

    vlan = db.get(Vlan, vlan_id)
    return VlanConfig.from_vlan(vlan)


def _resolve_tr069_olt_profile_id(db: Session, olt: object, pack: dict) -> int | None:
    raw_profile_id = pack.get("tr069_olt_profile_id")
    if raw_profile_id is not None:
        return raw_profile_id

    acs_server_id = getattr(olt, "tr069_acs_server_id", None)
    snapshot = getattr(olt, "tr069_profiles_snapshot", None) or {}
    profiles_payload = snapshot.get("profiles") if isinstance(snapshot, dict) else None
    if not acs_server_id or not isinstance(profiles_payload, list):
        return None

    from app.models.tr069 import Tr069AcsServer
    from app.services.network.tr069_profile_matching import match_tr069_profile

    server = db.get(Tr069AcsServer, acs_server_id)
    if server is None or not server.cwmp_url:
        return None
    profiles = [
        SimpleNamespace(
            profile_id=item.get("profile_id"),
            name=item.get("name", ""),
            acs_url=item.get("acs_url", ""),
            acs_username=item.get("acs_username", ""),
        )
        for item in profiles_payload
        if isinstance(item, dict)
    ]
    match = match_tr069_profile(
        profiles,
        acs_url=server.cwmp_url,
        acs_username=server.cwmp_username or "",
    )
    if match is None:
        return None
    try:
        return int(match.profile_id)
    except (TypeError, ValueError):
        return None


def resolve_olt_config_pack(
    db: Session,
    olt_id: str | UUID,
) -> OltConfigPack | None:
    """Build complete OLT config pack from OLT config_pack JSON field.

    Reads from the config_pack JSON column (source of truth) and resolves
    VLAN UUIDs to VlanConfig objects.

    Also checks OLT capability flags to gate unsupported commands:
    - supports_ont_internet_config=False → internet_config_ip_index=None
    - supports_ont_wan_config=False → wan_config_profile_id=None

    Args:
        db: Database session
        olt_id: OLT device ID (string or UUID)

    Returns:
        OltConfigPack with all resolved values, or None if OLT not found
    """
    from app.models.network import OLTDevice

    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        return None

    pack = olt.config_pack or {}
    tr069_olt_profile_id = _resolve_tr069_olt_profile_id(db, olt, pack)

    wan_provisioning_mode = str(
        getattr(olt, "wan_provisioning_mode", None) or "omci_wan_config"
    )
    supports_omci_wan = (
        wan_provisioning_mode == "omci_wan_config"
        and bool(getattr(olt, "supports_ont_internet_config", False))
        and bool(getattr(olt, "supports_ont_wan_config", False))
    )

    # Resolve internet_config_ip_index based on the OLT WAN provisioning strategy.
    # If the OLT doesn't support ont internet-config, force None regardless of pack value.
    internet_config_ip_index = (
        pack.get("internet_config_ip_index") if supports_omci_wan else None
    )

    # Resolve wan_config_profile_id based on OLT capability
    if supports_omci_wan:
        wan_config_profile_id = pack.get("wan_config_profile_id")
    else:
        # OLT doesn't support ont wan-config - force skip
        wan_config_profile_id = None

    return OltConfigPack(
        olt_id=str(olt.id),
        olt_name=olt.name or "",
        # Authorization profiles
        line_profile_id=pack.get("line_profile_id"),
        service_profile_id=pack.get("service_profile_id"),
        # TR-069 config (ACS server ID is still a FK on OLT, not in JSON)
        tr069_acs_server_id=(
            str(olt.tr069_acs_server_id) if olt.tr069_acs_server_id else None
        ),
        tr069_olt_profile_id=tr069_olt_profile_id,
        # VLANs (resolve UUID strings to VlanConfig)
        internet_vlan=_resolve_vlan(db, pack.get("internet_vlan_id")),
        management_vlan=_resolve_vlan(db, pack.get("management_vlan_id")),
        tr069_vlan=_resolve_vlan(db, pack.get("tr069_vlan_id")),
        voip_vlan=_resolve_vlan(db, pack.get("voip_vlan_id")),
        iptv_vlan=_resolve_vlan(db, pack.get("iptv_vlan_id")),
        # Provisioning knobs (capability-gated above)
        internet_config_ip_index=internet_config_ip_index,
        wan_config_profile_id=wan_config_profile_id,
        wan_provisioning_mode=wan_provisioning_mode,
        supports_ont_home_gateway_config=bool(
            getattr(olt, "supports_ont_home_gateway_config", False)
        ),
        # GEM indices
        internet_gem_index=pack.get("internet_gem_index"),
        mgmt_gem_index=pack.get("mgmt_gem_index"),
        voip_gem_index=pack.get("voip_gem_index"),
        iptv_gem_index=pack.get("iptv_gem_index"),
        # Connection request credentials
        cr_username=pack.get("cr_username"),
        cr_password=pack.get("cr_password"),
        # Traffic table indices
        mgmt_traffic_table_inbound=pack.get("mgmt_traffic_table_inbound"),
        mgmt_traffic_table_outbound=pack.get("mgmt_traffic_table_outbound"),
        internet_traffic_table_inbound=pack.get("internet_traffic_table_inbound"),
        internet_traffic_table_outbound=pack.get("internet_traffic_table_outbound"),
        # TR-069 WCD indices
        pppoe_wcd_index=pack.get("pppoe_wcd_index"),
        mgmt_wcd_index=pack.get("mgmt_wcd_index"),
        voip_wcd_index=pack.get("voip_wcd_index"),
    )


def get_olt_config_pack_or_raise(
    db: Session,
    olt_id: str | UUID,
) -> OltConfigPack:
    """Get OLT config pack, raising HTTPException if not found.

    Args:
        db: Database session
        olt_id: OLT device ID

    Returns:
        OltConfigPack

    Raises:
        HTTPException: If OLT not found
    """
    from fastapi import HTTPException

    config = resolve_olt_config_pack(db, olt_id)
    if config is None:
        raise HTTPException(status_code=404, detail="OLT device not found")
    return config


def validate_olt_config_pack(
    config: OltConfigPack,
    *,
    require_authorization: bool = True,
    require_vlans: bool = True,
    require_tr069: bool = False,
) -> list[str]:
    """Validate OLT config pack for provisioning readiness.

    Args:
        config: OLT config pack to validate
        require_authorization: Check line/service profiles
        require_vlans: Check internet/management VLANs
        require_tr069: Check TR-069 ACS and profile

    Returns:
        List of validation error messages (empty if valid)
    """
    errors: list[str] = []

    if require_authorization:
        if config.line_profile_id is None:
            errors.append("OLT missing default line profile ID")
        if config.service_profile_id is None:
            errors.append("OLT missing default service profile ID")

    if require_vlans:
        if config.internet_vlan.tag is None:
            errors.append("OLT missing internet VLAN")
        if config.management_vlan.tag is None:
            errors.append("OLT missing management VLAN")

    if require_tr069:
        if config.tr069_acs_server_id is None:
            errors.append("OLT missing TR-069 ACS server")
        if config.tr069_olt_profile_id is None:
            errors.append("OLT missing TR-069 OLT profile ID")

    return errors


@dataclass
class ConfigPackValidation:
    """Result of config pack validation with warnings and errors.

    Errors are blocking issues that prevent provisioning.
    Warnings are non-blocking issues that may cause problems.
    """

    is_valid: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_valid": self.is_valid,
            "has_warnings": self.has_warnings,
            "has_errors": self.has_errors,
            "warnings": self.warnings,
            "errors": self.errors,
            "warning_count": len(self.warnings),
            "error_count": len(self.errors),
        }


def _validate_gem_index(
    validation: ConfigPackValidation,
    *,
    name: str,
    value: int | str | None,
    minimum: int = 1,
    maximum: int = 8,
) -> None:
    if value is None:
        return
    try:
        gem_index = int(value)
    except (TypeError, ValueError):
        validation.is_valid = False
        validation.errors.append(f"{name} GEM index must be an integer")
        return
    if gem_index < minimum or gem_index > maximum:
        validation.is_valid = False
        validation.errors.append(
            f"{name} GEM index {gem_index} is outside supported range {minimum}-{maximum}"
        )


def validate_config_pack_comprehensive(
    db: Session,
    olt_id: str | UUID,
) -> ConfigPackValidation:
    """Comprehensive validation of OLT config pack for provisioning readiness.

    Checks all aspects of the config pack and returns warnings (non-blocking)
    and errors (blocking). Authorization can proceed with warnings but not errors.

    Required fields (ERROR if missing):
    - Authorization profiles (line/service)
    - Internet VLAN
    - Management VLAN
    - Management IP pool
    - TR-069 ACS server
    - TR-069 OLT profile ID

    Optional fields (WARNING if missing):
    - Connection request credentials

    Args:
        db: Database session
        olt_id: OLT device ID

    Returns:
        ConfigPackValidation with is_valid, warnings, and errors
    """
    from app.models.network import OLTDevice

    validation = ConfigPackValidation(is_valid=True)

    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        validation.is_valid = False
        validation.errors.append("OLT device not found")
        return validation

    config_pack = resolve_olt_config_pack(db, olt_id)
    if config_pack is None:
        validation.is_valid = False
        validation.errors.append("Failed to resolve OLT config pack")
        return validation

    # ========== ERRORS (blocking) ==========

    # Authorization profiles are required for ONT authorization
    if config_pack.line_profile_id is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing default line profile ID - ONTs cannot be authorized"
        )

    if config_pack.service_profile_id is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing default service profile ID - ONTs cannot be authorized"
        )

    # Internet VLAN is required for service ports
    if config_pack.internet_vlan.tag is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing internet VLAN - ONTs cannot receive internet service"
        )

    # TR-069 configuration is required for device management
    if config_pack.tr069_acs_server_id is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing TR-069 ACS server - ONTs cannot be managed remotely"
        )

    if config_pack.tr069_olt_profile_id is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing TR-069 OLT profile ID - ONTs cannot bind to ACS"
        )

    # Management VLAN is required for ACS connectivity
    if config_pack.management_vlan.tag is None:
        validation.is_valid = False
        validation.errors.append(
            "Missing management VLAN - ONTs cannot reach TR-069 ACS"
        )

    if not olt.mgmt_ip_pool_id:
        validation.is_valid = False
        validation.errors.append(
            "Missing management IP pool - ONTs cannot receive a managed ACS address"
        )

    _validate_gem_index(
        validation,
        name="Internet",
        value=config_pack.internet_gem_index,
    )
    _validate_gem_index(
        validation,
        name="Management",
        value=config_pack.mgmt_gem_index,
    )
    _validate_gem_index(validation, name="VoIP", value=config_pack.voip_gem_index)
    _validate_gem_index(validation, name="IPTV", value=config_pack.iptv_gem_index)

    if (
        config_pack.tr069_acs_server_id
        and config_pack.tr069_olt_profile_id is not None
        and config_pack.management_vlan.id
        and olt.mgmt_ip_pool_id
    ):
        from app.services.network.acs_reachability import (
            validate_olt_acs_management_reachability,
        )

        reachability_error = validate_olt_acs_management_reachability(
            db,
            {
                "tr069_acs_server_id": config_pack.tr069_acs_server_id,
                "default_tr069_olt_profile_id": config_pack.tr069_olt_profile_id,
                "management_vlan_id": config_pack.management_vlan.id,
                "mgmt_ip_pool_id": olt.mgmt_ip_pool_id,
            },
            current_olt=olt,
        )
        if reachability_error:
            validation.is_valid = False
            validation.errors.append(reachability_error)

    # ========== WARNINGS (non-blocking) ==========

    # Connection request credentials enable push notifications
    if not config_pack.cr_username or not config_pack.cr_password:
        validation.warnings.append(
            "Missing connection request credentials - ACS cannot push config changes"
        )

    return validation


def get_validation_summary(validation: ConfigPackValidation) -> str:
    """Get human-readable validation summary.

    Args:
        validation: ConfigPackValidation result

    Returns:
        Summary string for display
    """
    if validation.is_valid and not validation.has_warnings:
        return "Config pack is complete and ready for provisioning"

    if validation.is_valid and validation.has_warnings:
        return f"Config pack is valid with {len(validation.warnings)} warning(s)"

    return f"Config pack has {len(validation.errors)} error(s) that must be fixed"


# --------------------------------------------------------------------------
# Config pack JSON helpers
# --------------------------------------------------------------------------


def get_config_pack_value(
    olt: OLTDevice,
    key: str,
    default: object = None,
) -> object:
    """Read a value from OLT config_pack JSON.

    Args:
        olt: OLTDevice instance
        key: Key to read from config_pack
        default: Default value if key not present

    Returns:
        Value from config_pack or default
    """
    pack = olt.config_pack or {}
    return pack.get(key, default)


def set_config_pack_value(
    olt: OLTDevice,
    key: str,
    value: object,
) -> None:
    """Set a value in OLT config_pack JSON.

    Args:
        olt: OLTDevice instance
        key: Key to set in config_pack
        value: Value to set (None removes the key)
    """
    pack = dict(olt.config_pack or {})
    if value is None:
        pack.pop(key, None)
    else:
        pack[key] = value
    olt.config_pack = pack


def update_config_pack(
    olt: OLTDevice,
    updates: dict,
) -> None:
    """Bulk update OLT config_pack JSON.

    Args:
        olt: OLTDevice instance
        updates: Dictionary of key-value pairs to update
    """
    pack = dict(olt.config_pack or {})
    for key, value in updates.items():
        if value is None:
            pack.pop(key, None)
        else:
            pack[key] = value
    olt.config_pack = pack


# Required config_pack keys for provisioning
REQUIRED_CONFIG_PACK_KEYS = [
    "line_profile_id",
    "service_profile_id",
    "internet_vlan_id",
    "management_vlan_id",
    "tr069_olt_profile_id",
]


class ConfigPackIncompleteError(ValueError):
    """Raised when OLT config_pack is missing required fields."""

    def __init__(self, olt_name: str, missing_fields: list[str]):
        self.olt_name = olt_name
        self.missing_fields = missing_fields
        super().__init__(
            f"OLT '{olt_name}' config_pack missing required fields: {', '.join(missing_fields)}"
        )


def validate_config_pack_required(
    olt: OLTDevice,
    *,
    raise_on_error: bool = True,
) -> list[str]:
    """Validate that OLT config_pack has all required fields.

    Args:
        olt: OLTDevice instance
        raise_on_error: If True, raise ConfigPackIncompleteError on missing fields

    Returns:
        List of missing field names (empty if valid)

    Raises:
        ConfigPackIncompleteError: If raise_on_error=True and fields are missing
    """
    pack = olt.config_pack or {}
    missing = [key for key in REQUIRED_CONFIG_PACK_KEYS if not pack.get(key)]

    # Also check tr069_acs_server_id which is a FK on OLT, not in config_pack
    if olt.tr069_acs_server_id is None:
        missing.append("tr069_acs_server_id")
    if olt.mgmt_ip_pool_id is None:
        missing.append("mgmt_ip_pool_id")

    if missing and raise_on_error:
        raise ConfigPackIncompleteError(olt.name or str(olt.id), missing)

    return missing
