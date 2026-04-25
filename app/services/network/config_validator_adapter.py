"""Config Validator Adapter - Unified configuration validation.

Provides a single interface for validating network configurations before
they are applied to devices. Composes existing validators and adds
comprehensive checks for all configuration types.

For new code, use:
    from app.services.network.config_validator_adapter import (
        get_config_validator,
        validate_ont_config,
        validate_service_port_config,
    )

    validator = get_config_validator()
    result = validator.validate_ont_config(ont_config)
    if not result.is_valid:
        for error in result.errors:
            print(f"{error.field}: {error.message}")
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Constants
# ---------------------------------------------------------------------------


class ValidationSeverity(str, Enum):
    """Severity level for validation issues."""

    error = "error"
    warning = "warning"
    info = "info"


class ConfigType(str, Enum):
    """Type of configuration being validated."""

    ont = "ont"
    service_port = "service_port"
    management = "management"
    wifi = "wifi"
    pppoe = "pppoe"
    iphost = "iphost"
    vlan = "vlan"
    authorization = "authorization"


# Validation patterns
_FSP_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{1,3})$")
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9\-]{4,32}$")
_SSID_PATTERN = re.compile(r"^[\x20-\x7E]{1,32}$")  # Printable ASCII, 1-32 chars
_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")
_MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")

# Valid ranges
_VLAN_RANGE = range(1, 4095)
_ONT_ID_RANGE = range(0, 256)
_GEM_INDEX_RANGE = range(0, 256)
_PORT_NUMBER_RANGE = range(0, 256)
_WIFI_CHANNEL_2G = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0})  # 0=auto
_WIFI_CHANNEL_5G = frozenset({36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                               116, 120, 124, 128, 132, 136, 140, 144, 149, 153,
                               157, 161, 165, 0})  # 0=auto

# Security modes
_WIFI_SECURITY_MODES = frozenset({
    "None", "WEP-64", "WEP-128", "WPA-Personal", "WPA2-Personal",
    "WPA-WPA2-Personal", "WPA3-Personal", "WPA-Enterprise",
    "WPA2-Enterprise", "WPA3-Enterprise", "11i",  # Huawei alias
})

# Tag transform modes
_TAG_TRANSFORM_MODES = frozenset({"translate", "transparent", "default"})

# Characters that could be used for command injection
_DANGEROUS_CHARS = frozenset("\n\r;|&`$(){}[]<>\\\"'")


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue."""

    field: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.error
    code: str | None = None
    suggestion: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == ValidationSeverity.error

    @property
    def is_warning(self) -> bool:
        return self.severity == ValidationSeverity.warning


@dataclass
class ConfigValidationResult:
    """Result of configuration validation."""

    config_type: ConfigType
    is_valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    validated_data: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Get only error-level issues."""
        return [i for i in self.issues if i.severity == ValidationSeverity.error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Get only warning-level issues."""
        return [i for i in self.issues if i.severity == ValidationSeverity.warning]

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def has_blocking_issues(self) -> bool:
        """Check if there are any errors that should block the operation."""
        return self.error_count > 0

    def add_error(
        self,
        field: str,
        message: str,
        *,
        code: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        """Add an error issue."""
        self.issues.append(ValidationIssue(
            field=field,
            message=message,
            severity=ValidationSeverity.error,
            code=code,
            suggestion=suggestion,
        ))
        self.is_valid = False

    def add_warning(
        self,
        field: str,
        message: str,
        *,
        code: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        """Add a warning issue."""
        self.issues.append(ValidationIssue(
            field=field,
            message=message,
            severity=ValidationSeverity.warning,
            code=code,
            suggestion=suggestion,
        ))

    def add_info(
        self,
        field: str,
        message: str,
    ) -> None:
        """Add an info-level issue."""
        self.issues.append(ValidationIssue(
            field=field,
            message=message,
            severity=ValidationSeverity.info,
        ))

    def merge(self, other: ConfigValidationResult) -> None:
        """Merge another result into this one."""
        self.issues.extend(other.issues)
        self.validated_data.update(other.validated_data)
        if not other.is_valid:
            self.is_valid = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "config_type": self.config_type.value,
            "is_valid": self.is_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [
                {
                    "field": i.field,
                    "message": i.message,
                    "severity": i.severity.value,
                    "code": i.code,
                    "suggestion": i.suggestion,
                }
                for i in self.issues
            ],
        }


# ---------------------------------------------------------------------------
# Config Data Classes
# ---------------------------------------------------------------------------


@dataclass
class OntConfig:
    """ONT configuration to validate."""

    serial_number: str | None = None
    fsp: str | None = None
    ont_id: int | None = None
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    description: str | None = None


@dataclass
class ServicePortConfig:
    """Service port configuration to validate."""

    fsp: str | None = None
    ont_id: int | None = None
    vlan_id: int | None = None
    user_vlan: int | None = None
    gem_index: int | None = None
    tag_transform: str = "translate"
    port_index: int | None = None


@dataclass
class ManagementConfig:
    """Management IP configuration to validate."""

    vlan_id: int | None = None
    ip_mode: str = "dhcp"  # dhcp, static, pppoe
    ip_address: str | None = None
    subnet_mask: str | None = None
    gateway: str | None = None
    priority: int = 0


@dataclass
class WifiConfig:
    """WiFi configuration to validate."""

    enabled: bool = True
    ssid: str | None = None
    password: str | None = None
    security_mode: str = "WPA2-Personal"
    channel: int = 0  # 0 = auto
    band: str = "2.4GHz"  # 2.4GHz, 5GHz


@dataclass
class PppoeConfig:
    """PPPoE configuration to validate."""

    username: str | None = None
    password: str | None = None
    vlan_id: int | None = None
    service_name: str | None = None


@dataclass
class AuthorizationConfig:
    """ONT authorization configuration to validate."""

    serial_number: str | None = None
    fsp: str | None = None
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    force_reauthorize: bool = False


# ---------------------------------------------------------------------------
# Protocol Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class ConfigValidator(Protocol):
    """Protocol for configuration validators."""

    def validate_ont_config(
        self,
        config: OntConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate ONT configuration."""
        ...

    def validate_service_port_config(
        self,
        config: ServicePortConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate service port configuration."""
        ...

    def validate_management_config(
        self,
        config: ManagementConfig,
        *,
        db: Session | None = None,
    ) -> ConfigValidationResult:
        """Validate management IP configuration."""
        ...

    def validate_wifi_config(
        self,
        config: WifiConfig,
    ) -> ConfigValidationResult:
        """Validate WiFi configuration."""
        ...

    def validate_pppoe_config(
        self,
        config: PppoeConfig,
        *,
        db: Session | None = None,
    ) -> ConfigValidationResult:
        """Validate PPPoE configuration."""
        ...

    def validate_authorization_config(
        self,
        config: AuthorizationConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate ONT authorization configuration."""
        ...


# ---------------------------------------------------------------------------
# Base Validator Implementation
# ---------------------------------------------------------------------------


class BaseConfigValidator:
    """Base configuration validator with common validation logic."""

    # ========== Primitive Validators ==========

    def _validate_fsp(self, fsp: str | None, result: ConfigValidationResult) -> str | None:
        """Validate frame/slot/port format."""
        if not fsp:
            result.add_error("fsp", "Frame/Slot/Port is required")
            return None

        fsp = fsp.strip()

        if any(c in fsp for c in _DANGEROUS_CHARS):
            result.add_error("fsp", "FSP contains invalid characters", code="INVALID_CHARS")
            return None

        match = _FSP_PATTERN.match(fsp)
        if not match:
            result.add_error(
                "fsp",
                f"Invalid FSP format '{fsp}'. Expected format: frame/slot/port (e.g., 0/1/0)",
                code="INVALID_FORMAT",
            )
            return None

        frame, slot, port = int(match.group(1)), int(match.group(2)), int(match.group(3))

        # Range checks
        if frame > 15:
            result.add_warning("fsp", f"Unusual frame number {frame} (typically 0-15)")
        if slot > 20:
            result.add_warning("fsp", f"Unusual slot number {slot} (typically 0-20)")
        if port > 16:
            result.add_warning("fsp", f"Unusual port number {port} (typically 0-16)")

        result.validated_data["fsp"] = fsp
        result.validated_data["frame"] = frame
        result.validated_data["slot"] = slot
        result.validated_data["port"] = port

        return fsp

    def _validate_serial(
        self, serial: str | None, result: ConfigValidationResult
    ) -> str | None:
        """Validate ONT serial number."""
        if not serial:
            result.add_error("serial_number", "Serial number is required")
            return None

        serial = serial.strip().upper()

        if any(c in serial for c in _DANGEROUS_CHARS):
            result.add_error(
                "serial_number", "Serial contains invalid characters", code="INVALID_CHARS"
            )
            return None

        if not _SERIAL_PATTERN.match(serial):
            result.add_error(
                "serial_number",
                f"Invalid serial format '{serial}'. Expected 4-32 alphanumeric characters.",
                code="INVALID_FORMAT",
            )
            return None

        # Check for common prefixes
        known_prefixes = {"HWTC", "ZTEG", "ALCL", "GPON", "UBNT"}
        if len(serial) >= 4 and serial[:4] not in known_prefixes:
            result.add_info("serial_number", f"Unusual vendor prefix: {serial[:4]}")

        result.validated_data["serial_number"] = serial
        return serial

    def _validate_vlan(
        self,
        vlan: int | str | None,
        field_name: str,
        result: ConfigValidationResult,
        *,
        allow_none: bool = False,
    ) -> int | None:
        """Validate VLAN ID."""
        if vlan is None:
            if not allow_none:
                result.add_error(field_name, "VLAN ID is required")
            return None

        try:
            vlan_int = int(vlan)
        except (ValueError, TypeError):
            result.add_error(field_name, f"VLAN must be a number, got '{vlan}'")
            return None

        if vlan_int not in _VLAN_RANGE:
            result.add_error(
                field_name,
                f"VLAN {vlan_int} out of range. Must be 1-4094.",
                code="OUT_OF_RANGE",
            )
            return None

        # Reserved VLANs warning
        if vlan_int == 1:
            result.add_warning(
                field_name,
                "VLAN 1 is the default VLAN and may cause issues",
                suggestion="Consider using a different VLAN",
            )
        elif vlan_int >= 4000:
            result.add_warning(
                field_name,
                f"VLAN {vlan_int} is in reserved range (4000-4094)",
            )

        result.validated_data[field_name] = vlan_int
        return vlan_int

    def _validate_ont_id(
        self, ont_id: int | None, result: ConfigValidationResult
    ) -> int | None:
        """Validate ONT ID."""
        if ont_id is None:
            result.add_error("ont_id", "ONT ID is required")
            return None

        if ont_id not in _ONT_ID_RANGE:
            result.add_error(
                "ont_id",
                f"ONT ID {ont_id} out of range. Must be 0-255.",
                code="OUT_OF_RANGE",
            )
            return None

        result.validated_data["ont_id"] = ont_id
        return ont_id

    def _validate_gem_index(
        self,
        gem: int | str | None,
        result: ConfigValidationResult,
        *,
        allow_none: bool = False,
    ) -> int | None:
        """Validate GEM port index."""
        if gem is None:
            if not allow_none:
                result.add_error("gem_index", "GEM index is required")
            return None

        try:
            gem_int = int(gem)
        except (ValueError, TypeError):
            result.add_error("gem_index", f"GEM index must be a number, got '{gem}'")
            return None

        if gem_int not in _GEM_INDEX_RANGE:
            result.add_error(
                "gem_index",
                f"GEM index {gem_int} out of range. Must be 0-255.",
                code="OUT_OF_RANGE",
            )
            return None

        result.validated_data["gem_index"] = gem_int
        return gem_int

    def _validate_ip_address(
        self,
        ip: str | None,
        field_name: str,
        result: ConfigValidationResult,
        *,
        allow_none: bool = False,
    ) -> str | None:
        """Validate IP address."""
        if not ip:
            if not allow_none:
                result.add_error(field_name, "IP address is required")
            return None

        ip = ip.strip()

        if any(c in ip for c in _DANGEROUS_CHARS):
            result.add_error(field_name, "IP address contains invalid characters")
            return None

        try:
            addr = ipaddress.ip_address(ip)
            normalized = str(addr)
            result.validated_data[field_name] = normalized
            return normalized
        except ValueError:
            result.add_error(field_name, f"Invalid IP address: {ip}")
            return None

    def _validate_subnet_mask(
        self,
        mask: str | None,
        result: ConfigValidationResult,
        *,
        allow_none: bool = False,
    ) -> str | None:
        """Validate subnet mask."""
        if not mask:
            if not allow_none:
                result.add_error("subnet_mask", "Subnet mask is required")
            return None

        mask = mask.strip()

        if any(c in mask for c in _DANGEROUS_CHARS):
            result.add_error("subnet_mask", "Subnet mask contains invalid characters")
            return None

        # Validate as IP address format
        try:
            ipaddress.ip_address(mask)
        except ValueError:
            result.add_error("subnet_mask", f"Invalid subnet mask format: {mask}")
            return None

        # Verify contiguous mask
        parts = mask.split(".")
        if len(parts) != 4:
            result.add_error("subnet_mask", "Subnet mask must be in dotted-decimal format")
            return None

        try:
            binary = "".join(format(int(p), "08b") for p in parts)
            if "01" in binary:  # Invalid: 0 followed by 1
                result.add_error(
                    "subnet_mask",
                    f"Invalid subnet mask: {mask} (must be contiguous)",
                )
                return None
        except ValueError:
            result.add_error("subnet_mask", f"Invalid subnet mask: {mask}")
            return None

        result.validated_data["subnet_mask"] = mask
        return mask

    def _validate_ssid(
        self, ssid: str | None, result: ConfigValidationResult
    ) -> str | None:
        """Validate WiFi SSID."""
        if not ssid:
            result.add_error("ssid", "SSID is required")
            return None

        ssid = ssid.strip()

        if len(ssid) > 32:
            result.add_error("ssid", f"SSID too long ({len(ssid)} chars). Maximum is 32.")
            return None

        if len(ssid) < 1:
            result.add_error("ssid", "SSID cannot be empty")
            return None

        if not _SSID_PATTERN.match(ssid):
            result.add_error(
                "ssid",
                "SSID contains invalid characters. Use printable ASCII only.",
            )
            return None

        result.validated_data["ssid"] = ssid
        return ssid

    def _validate_wifi_password(
        self, password: str | None, security_mode: str, result: ConfigValidationResult
    ) -> str | None:
        """Validate WiFi password."""
        # No password needed for open networks
        if security_mode.lower() in ("none", "open"):
            if password:
                result.add_warning(
                    "password",
                    "Password provided but security mode is 'None'",
                )
            return None

        if not password:
            result.add_error("password", "Password is required for secured networks")
            return None

        # WPA password length requirements
        if "WPA" in security_mode.upper() or security_mode == "11i":
            if len(password) < 8:
                result.add_error(
                    "password",
                    f"Password too short ({len(password)} chars). WPA requires minimum 8 characters.",
                )
                return None
            if len(password) > 63:
                result.add_error(
                    "password",
                    f"Password too long ({len(password)} chars). Maximum is 63 characters.",
                )
                return None

        result.validated_data["password"] = password
        return password

    def _validate_security_mode(
        self, mode: str | None, result: ConfigValidationResult
    ) -> str | None:
        """Validate WiFi security mode."""
        if not mode:
            result.add_error("security_mode", "Security mode is required")
            return None

        mode = mode.strip()

        if mode not in _WIFI_SECURITY_MODES:
            result.add_error(
                "security_mode",
                f"Invalid security mode '{mode}'. Valid modes: {', '.join(sorted(_WIFI_SECURITY_MODES))}",
            )
            return None

        # Deprecation warnings
        if mode.startswith("WEP"):
            result.add_warning(
                "security_mode",
                f"{mode} is deprecated and insecure",
                suggestion="Use WPA2-Personal or WPA3-Personal instead",
            )
        elif mode == "WPA-Personal":
            result.add_warning(
                "security_mode",
                "WPA-Personal is deprecated",
                suggestion="Use WPA2-Personal for better security",
            )

        result.validated_data["security_mode"] = mode
        return mode

    def _validate_wifi_channel(
        self, channel: int | None, band: str, result: ConfigValidationResult
    ) -> int | None:
        """Validate WiFi channel for given band."""
        if channel is None:
            channel = 0  # Auto

        try:
            channel = int(channel)
        except (ValueError, TypeError):
            result.add_error("channel", f"Channel must be a number, got '{channel}'")
            return None

        valid_channels = _WIFI_CHANNEL_5G if "5" in band else _WIFI_CHANNEL_2G

        if channel not in valid_channels:
            result.add_error(
                "channel",
                f"Invalid channel {channel} for {band}. Valid channels: {sorted(valid_channels)}",
            )
            return None

        result.validated_data["channel"] = channel
        return channel


# ---------------------------------------------------------------------------
# Network Config Validator
# ---------------------------------------------------------------------------


class NetworkConfigValidator(BaseConfigValidator):
    """Full configuration validator for network operations."""

    def validate_ont_config(
        self,
        config: OntConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate ONT configuration."""
        result = ConfigValidationResult(config_type=ConfigType.ont)

        self._validate_serial(config.serial_number, result)
        self._validate_fsp(config.fsp, result)

        if config.ont_id is not None:
            self._validate_ont_id(config.ont_id, result)

        # Profile validation
        if config.line_profile_id is not None:
            if config.line_profile_id < 0:
                result.add_error("line_profile_id", "Line profile ID must be non-negative")

        if config.service_profile_id is not None:
            if config.service_profile_id < 0:
                result.add_error("service_profile_id", "Service profile ID must be non-negative")

        # Description validation
        if config.description:
            if any(c in config.description for c in _DANGEROUS_CHARS):
                result.add_error("description", "Description contains invalid characters")
            elif len(config.description) > 64:
                result.add_error("description", "Description too long (max 64 characters)")

        return result

    def validate_service_port_config(
        self,
        config: ServicePortConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate service port configuration."""
        result = ConfigValidationResult(config_type=ConfigType.service_port)

        self._validate_fsp(config.fsp, result)

        if config.ont_id is not None:
            self._validate_ont_id(config.ont_id, result)

        self._validate_vlan(config.vlan_id, "vlan_id", result)
        self._validate_vlan(config.user_vlan, "user_vlan", result, allow_none=True)
        self._validate_gem_index(config.gem_index, result)

        # Tag transform validation
        if config.tag_transform:
            if config.tag_transform not in _TAG_TRANSFORM_MODES:
                result.add_error(
                    "tag_transform",
                    f"Invalid tag transform '{config.tag_transform}'. "
                    f"Valid modes: {', '.join(_TAG_TRANSFORM_MODES)}",
                )
            else:
                result.validated_data["tag_transform"] = config.tag_transform

        # Port index validation (optional)
        if config.port_index is not None:
            if config.port_index < 0 or config.port_index > 65535:
                result.add_error("port_index", "Port index out of range (0-65535)")
            else:
                result.validated_data["port_index"] = config.port_index

        # Cross-field validation
        if config.user_vlan is not None and config.vlan_id is not None:
            if config.tag_transform == "transparent" and config.user_vlan != config.vlan_id:
                result.add_warning(
                    "tag_transform",
                    "Transparent mode with different user_vlan may cause issues",
                )

        # DB validation if available
        if db and config.vlan_id:
            self._validate_vlan_exists(db, config.vlan_id, result)

        return result

    def validate_management_config(
        self,
        config: ManagementConfig,
        *,
        db: Session | None = None,
    ) -> ConfigValidationResult:
        """Validate management IP configuration."""
        result = ConfigValidationResult(config_type=ConfigType.management)

        self._validate_vlan(config.vlan_id, "vlan_id", result)

        # IP mode validation
        valid_modes = {"dhcp", "static", "pppoe"}
        if config.ip_mode not in valid_modes:
            result.add_error(
                "ip_mode",
                f"Invalid IP mode '{config.ip_mode}'. Valid modes: {', '.join(valid_modes)}",
            )
        else:
            result.validated_data["ip_mode"] = config.ip_mode

        # Static IP requires address configuration
        if config.ip_mode == "static":
            self._validate_ip_address(config.ip_address, "ip_address", result)
            self._validate_subnet_mask(config.subnet_mask, result)
            self._validate_ip_address(config.gateway, "gateway", result, allow_none=True)

            # Cross-validate IP and gateway are in same subnet
            if (config.ip_address and config.subnet_mask and config.gateway and
                    result.validated_data.get("ip_address") and
                    result.validated_data.get("gateway")):
                try:
                    ip = ipaddress.ip_address(config.ip_address)
                    gw = ipaddress.ip_address(config.gateway)
                    mask = config.subnet_mask
                    network = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
                    if gw not in network:
                        result.add_error(
                            "gateway",
                            f"Gateway {gw} is not in the same subnet as {ip}/{mask}",
                        )
                except ValueError:
                    pass  # Already reported as invalid

        # Priority validation
        if config.priority < 0 or config.priority > 7:
            result.add_error("priority", "Priority must be 0-7")
        else:
            result.validated_data["priority"] = config.priority

        return result

    def validate_wifi_config(
        self,
        config: WifiConfig,
    ) -> ConfigValidationResult:
        """Validate WiFi configuration."""
        result = ConfigValidationResult(config_type=ConfigType.wifi)

        result.validated_data["enabled"] = config.enabled

        if config.enabled:
            self._validate_ssid(config.ssid, result)
            self._validate_security_mode(config.security_mode, result)
            self._validate_wifi_password(config.password, config.security_mode, result)
            self._validate_wifi_channel(config.channel, config.band, result)

            # Band validation
            if config.band not in {"2.4GHz", "5GHz", "6GHz", "auto"}:
                result.add_error(
                    "band",
                    f"Invalid band '{config.band}'. Valid: 2.4GHz, 5GHz, 6GHz, auto",
                )
            else:
                result.validated_data["band"] = config.band

        return result

    def validate_pppoe_config(
        self,
        config: PppoeConfig,
        *,
        db: Session | None = None,
    ) -> ConfigValidationResult:
        """Validate PPPoE configuration."""
        result = ConfigValidationResult(config_type=ConfigType.pppoe)

        # Username validation
        if not config.username:
            result.add_error("username", "PPPoE username is required")
        else:
            username = config.username.strip()
            if any(c in username for c in _DANGEROUS_CHARS):
                result.add_error("username", "Username contains invalid characters")
            elif len(username) > 64:
                result.add_error("username", "Username too long (max 64 characters)")
            else:
                result.validated_data["username"] = username

        # Password validation
        if not config.password:
            result.add_error("password", "PPPoE password is required")
        else:
            if len(config.password) > 64:
                result.add_error("password", "Password too long (max 64 characters)")
            else:
                result.validated_data["password"] = config.password

        # VLAN validation (optional for PPPoE)
        if config.vlan_id is not None:
            self._validate_vlan(config.vlan_id, "vlan_id", result)

        # Service name validation (optional)
        if config.service_name:
            if any(c in config.service_name for c in _DANGEROUS_CHARS):
                result.add_error("service_name", "Service name contains invalid characters")
            else:
                result.validated_data["service_name"] = config.service_name

        return result

    def validate_authorization_config(
        self,
        config: AuthorizationConfig,
        *,
        db: Session | None = None,
        olt: OLTDevice | None = None,
    ) -> ConfigValidationResult:
        """Validate ONT authorization configuration."""
        result = ConfigValidationResult(config_type=ConfigType.authorization)

        self._validate_serial(config.serial_number, result)
        self._validate_fsp(config.fsp, result)

        # Profile validation
        if config.line_profile_id is not None:
            if config.line_profile_id < 0:
                result.add_error("line_profile_id", "Line profile ID must be non-negative")
            else:
                result.validated_data["line_profile_id"] = config.line_profile_id

        if config.service_profile_id is not None:
            if config.service_profile_id < 0:
                result.add_error("service_profile_id", "Service profile ID must be non-negative")
            else:
                result.validated_data["service_profile_id"] = config.service_profile_id

        result.validated_data["force_reauthorize"] = config.force_reauthorize

        # OLT-specific validation
        if olt and db:
            self._validate_olt_has_vendor_model(olt, result)
            self._validate_olt_has_credentials(olt, result)
            self._validate_olt_has_authorization_profiles(db, olt, result)
            if config.fsp:
                self._validate_pon_port_exists(db, olt, config.fsp, result)

        return result

    # ========== Database Validators ==========

    def _validate_vlan_exists(
        self,
        db: Session,
        vlan_id: int,
        result: ConfigValidationResult,
    ) -> None:
        """Check if VLAN exists in database."""
        try:
            from sqlalchemy import select

            from app.models.network import Vlan

            stmt = select(Vlan).where(Vlan.vlan_id == vlan_id)
            vlan = db.scalars(stmt).first()

            if not vlan:
                result.add_warning(
                    "vlan_id",
                    f"VLAN {vlan_id} not found in database",
                    suggestion="Create the VLAN before using it",
                )
            elif not getattr(vlan, "is_active", True):
                result.add_warning(
                    "vlan_id",
                    f"VLAN {vlan_id} is inactive",
                )
        except Exception as exc:
            logger.debug("VLAN validation skipped: %s", exc)

    def _validate_olt_has_credentials(
        self,
        olt: OLTDevice,
        result: ConfigValidationResult,
    ) -> None:
        """Check if OLT has SSH credentials configured (required for authorization)."""
        # Management address is required for SSH connection
        if not getattr(olt, "mgmt_ip", None) and not getattr(olt, "hostname", None):
            result.add_error(
                "olt",
                "OLT management IP or hostname is required for authorization",
                code="NO_MGMT_ADDRESS",
            )
        if not getattr(olt, "ssh_username", None):
            result.add_error(
                "olt",
                "OLT SSH username is required for authorization",
                code="NO_SSH_USERNAME",
            )
        if not getattr(olt, "ssh_password", None):
            result.add_error(
                "olt",
                "OLT SSH password is required for authorization",
                code="NO_SSH_PASSWORD",
            )

    def _validate_olt_has_vendor_model(
        self,
        olt: OLTDevice,
        result: ConfigValidationResult,
    ) -> None:
        """Check if OLT has vendor and model configured (required for authorization)."""
        if not getattr(olt, "vendor", None):
            result.add_error(
                "olt",
                "OLT vendor is required for authorization",
                code="NO_VENDOR",
            )
        if not getattr(olt, "model", None):
            result.add_error(
                "olt",
                "OLT model is required for authorization",
                code="NO_MODEL",
            )

    def _validate_olt_has_authorization_profiles(
        self,
        db: Session,
        olt: OLTDevice,
        result: ConfigValidationResult,
    ) -> None:
        """Check if OLT has default authorization profiles."""
        del db
        if not getattr(olt, "default_line_profile_id", None):
            result.add_error(
                "olt",
                f"OLT '{olt.name}' has no default authorization line profile.",
                code="NO_DEFAULT_LINE_PROFILE",
            )
        if not getattr(olt, "default_service_profile_id", None):
            result.add_error(
                "olt",
                f"OLT '{olt.name}' has no default authorization service profile.",
                code="NO_DEFAULT_SERVICE_PROFILE",
            )

    def _validate_pon_port_exists(
        self,
        db: Session,
        olt: OLTDevice,
        fsp: str,
        result: ConfigValidationResult,
    ) -> None:
        """Check if PON port exists on OLT."""
        try:
            from sqlalchemy import select

            from app.models.network import PonPort

            stmt = select(PonPort).where(
                PonPort.olt_device_id == olt.id,
                PonPort.fsp == fsp,
            )
            port = db.scalars(stmt).first()

            if not port:
                result.add_info(
                    "fsp",
                    f"PON port {fsp} not yet registered on this OLT",
                )
        except Exception as exc:
            logger.debug("PON port validation skipped: %s", exc)


# ---------------------------------------------------------------------------
# Factory and Convenience Functions
# ---------------------------------------------------------------------------

_validator_instance: ConfigValidator | None = None


def get_config_validator() -> ConfigValidator:
    """Get the configuration validator singleton."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = NetworkConfigValidator()
    return _validator_instance


# Convenience functions
def validate_ont_config(
    config: OntConfig,
    *,
    db: Session | None = None,
    olt: OLTDevice | None = None,
) -> ConfigValidationResult:
    """Validate ONT configuration."""
    return get_config_validator().validate_ont_config(config, db=db, olt=olt)


def validate_service_port_config(
    config: ServicePortConfig,
    *,
    db: Session | None = None,
    olt: OLTDevice | None = None,
) -> ConfigValidationResult:
    """Validate service port configuration."""
    return get_config_validator().validate_service_port_config(config, db=db, olt=olt)


def validate_management_config(
    config: ManagementConfig,
    *,
    db: Session | None = None,
) -> ConfigValidationResult:
    """Validate management IP configuration."""
    return get_config_validator().validate_management_config(config, db=db)


def validate_wifi_config(config: WifiConfig) -> ConfigValidationResult:
    """Validate WiFi configuration."""
    return get_config_validator().validate_wifi_config(config)


def validate_pppoe_config(
    config: PppoeConfig,
    *,
    db: Session | None = None,
) -> ConfigValidationResult:
    """Validate PPPoE configuration."""
    return get_config_validator().validate_pppoe_config(config, db=db)


def validate_authorization_config(
    config: AuthorizationConfig,
    *,
    db: Session | None = None,
    olt: OLTDevice | None = None,
) -> ConfigValidationResult:
    """Validate ONT authorization configuration."""
    return get_config_validator().validate_authorization_config(config, db=db, olt=olt)


# ---------------------------------------------------------------------------
# Quick Validators (for simple cases)
# ---------------------------------------------------------------------------


def quick_validate_fsp(fsp: str) -> tuple[bool, str | None]:
    """Quick FSP validation without full result object."""
    if not fsp:
        return False, "FSP is required"
    if any(c in fsp for c in _DANGEROUS_CHARS):
        return False, "FSP contains invalid characters"
    if not _FSP_PATTERN.match(fsp.strip()):
        return False, f"Invalid FSP format: {fsp}"
    return True, None


def quick_validate_serial(serial: str) -> tuple[bool, str | None]:
    """Quick serial validation without full result object."""
    if not serial:
        return False, "Serial is required"
    serial = serial.strip().upper()
    if any(c in serial for c in _DANGEROUS_CHARS):
        return False, "Serial contains invalid characters"
    if not _SERIAL_PATTERN.match(serial):
        return False, f"Invalid serial format: {serial}"
    return True, None


def quick_validate_vlan(vlan: int | str) -> tuple[bool, str | None]:
    """Quick VLAN validation without full result object."""
    try:
        vlan_int = int(vlan)
    except (ValueError, TypeError):
        return False, "VLAN must be a number"
    if vlan_int not in _VLAN_RANGE:
        return False, f"VLAN {vlan_int} out of range (1-4094)"
    return True, None
