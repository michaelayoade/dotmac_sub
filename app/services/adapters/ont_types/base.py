"""ONT type adapter base class and registry.

Code adapters provide device-specific transforms (security mode mappings, etc.)
that can't be stored as simple strings in the database.

TR-069 parameter paths are stored in the OnuType database table.
The adapter_name field on OnuType links to these code adapters for transforms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OntTypeAdapter:
    """Code adapter for ONT model-specific transforms.

    Provides device-specific value transformations that can't be stored
    in the database (e.g., security mode mappings).

    TR-069 paths are stored in the OnuType database table, not here.
    """

    # Identity - must match OnuType.adapter_name
    name: str

    # Vendor for documentation
    vendor: str

    # Security mode mappings (input -> device-specific value)
    # e.g., {"WPA2": "11i", "WPA": "WPA", "None": "None"}
    security_mode_map: dict[str, str] = field(default_factory=dict)

    # Notes/documentation
    notes: str | None = None

    def transform_security_mode(self, mode: str) -> str:
        """Transform security mode to device-specific value.

        Args:
            mode: Standard security mode (e.g., "WPA2", "WPA2-Personal")

        Returns:
            Device-specific value (e.g., "11i" for TR-098 BeaconType)
        """
        return self.security_mode_map.get(mode, mode)


class OntTypeRegistry:
    """Registry for ONT type adapters (transforms only)."""

    def __init__(self) -> None:
        self._adapters: dict[str, OntTypeAdapter] = {}

    def register(self, adapter: OntTypeAdapter) -> OntTypeAdapter:
        """Register an ONT type adapter."""
        if adapter.name in self._adapters:
            logger.warning("Overwriting ONT type adapter: %s", adapter.name)
        self._adapters[adapter.name] = adapter
        logger.debug("Registered ONT type adapter: %s", adapter.name)
        return adapter

    def get(self, name: str | None) -> OntTypeAdapter | None:
        """Get adapter by name. Returns None if name is None or not found."""
        if not name:
            return None
        return self._adapters.get(name)

    def names(self) -> list[str]:
        """Return all registered adapter names."""
        return sorted(self._adapters.keys())

    def all(self) -> list[OntTypeAdapter]:
        """Return all registered adapters."""
        return list(self._adapters.values())


# Global registry instance
ont_type_registry = OntTypeRegistry()
