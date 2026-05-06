"""OLT type adapter base class and registry.

OLT adapters define firmware-specific capabilities that gate which
CLI commands are attempted during ONT provisioning.

Key capabilities:
- supports_ont_internet_config: MA5608T V800R013 does NOT support this
- supports_ont_wan_config: MA5608T V800R013 does NOT support this
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OltCapabilities:
    """Firmware-specific OLT command capabilities."""

    # OMCI provisioning commands
    supports_ont_internet_config: bool = True
    supports_ont_wan_config: bool = True

    # Other capability flags can be added here
    supports_ont_wifi_config: bool = False  # MA5800 V100R019+ only
    supports_ont_port_vlan: bool = True
    supports_traffic_table: bool = True


@dataclass
class OltTypeAdapter:
    """Adapter for OLT model/firmware-specific behavior.

    Provides capability flags that gate which CLI commands are
    attempted during ONT provisioning.
    """

    # Identity
    name: str
    vendor: str

    # Model patterns (substring match)
    model_patterns: list[str] = field(default_factory=list)

    # Firmware version patterns (regex)
    # e.g., r"V800R013.*" matches V800R013C00, V800R013C10, etc.
    firmware_patterns: list[str] = field(default_factory=list)

    # Capabilities
    capabilities: OltCapabilities = field(default_factory=OltCapabilities)

    # Notes
    notes: str | None = None

    def matches_model(self, model: str | None) -> bool:
        """Check if this adapter matches the given model."""
        if not model or not self.model_patterns:
            return False
        model_upper = model.upper()
        return any(p.upper() in model_upper for p in self.model_patterns)

    def matches_firmware(self, firmware: str | None) -> bool:
        """Check if this adapter matches the given firmware version."""
        if not firmware or not self.firmware_patterns:
            return False
        for pattern in self.firmware_patterns:
            if re.search(pattern, firmware, re.IGNORECASE):
                return True
        return False

    def matches(
        self,
        *,
        model: str | None = None,
        firmware: str | None = None,
    ) -> bool:
        """Check if this adapter matches the given model AND firmware.

        Both must match if both are provided. If only one is provided,
        only that one needs to match.
        """
        model_ok = not model or not self.model_patterns or self.matches_model(model)
        firmware_ok = not firmware or not self.firmware_patterns or self.matches_firmware(firmware)

        # If adapter has both patterns, both must match
        if self.model_patterns and self.firmware_patterns:
            return self.matches_model(model) and self.matches_firmware(firmware)

        # Otherwise, check what's available
        if self.model_patterns and model:
            return self.matches_model(model)
        if self.firmware_patterns and firmware:
            return self.matches_firmware(firmware)

        return False


class OltTypeRegistry:
    """Registry for OLT type adapters."""

    def __init__(self) -> None:
        self._adapters: list[OltTypeAdapter] = []

    def register(self, adapter: OltTypeAdapter) -> OltTypeAdapter:
        """Register an OLT type adapter.

        Adapters are checked in registration order - more specific
        adapters should be registered first.
        """
        self._adapters.append(adapter)
        logger.debug("Registered OLT type adapter: %s", adapter.name)
        return adapter

    def find(
        self,
        *,
        model: str | None = None,
        firmware: str | None = None,
    ) -> OltTypeAdapter | None:
        """Find adapter matching OLT model/firmware.

        Returns first matching adapter or None.
        """
        for adapter in self._adapters:
            if adapter.matches(model=model, firmware=firmware):
                return adapter
        return None

    def get_capabilities(
        self,
        *,
        model: str | None = None,
        firmware: str | None = None,
    ) -> OltCapabilities:
        """Get capabilities for an OLT based on model/firmware.

        Returns matched adapter's capabilities, or default capabilities
        (all features enabled) if no match.
        """
        adapter = self.find(model=model, firmware=firmware)
        if adapter:
            return adapter.capabilities
        # Default: all capabilities enabled
        return OltCapabilities()

    def names(self) -> list[str]:
        """Return all registered adapter names."""
        return [a.name for a in self._adapters]


# Global registry instance
olt_type_registry = OltTypeRegistry()
