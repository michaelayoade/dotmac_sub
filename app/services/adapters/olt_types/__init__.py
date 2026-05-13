"""OLT type adapters registry.

Provides firmware-specific capability flags for OLT provisioning.

Usage:
    from app.services.adapters.olt_types import olt_type_registry

    # Get capabilities for an OLT
    caps = olt_type_registry.get_capabilities(
        model="MA5608T",
        firmware="V800R013C00"
    )

    if caps.supports_ont_internet_config:
        # Generate ont internet-config command
        pass
    else:
        # Skip - use TR-069 only path
        pass
"""

# Import vendor modules to register adapters
from app.services.adapters.olt_types import huawei  # noqa: F401
from app.services.adapters.olt_types.base import (
    OltCapabilities,
    OltTypeAdapter,
    OltTypeRegistry,
    WanProvisioningMode,
    olt_type_registry,
)

__all__ = [
    "OltCapabilities",
    "OltTypeAdapter",
    "OltTypeRegistry",
    "WanProvisioningMode",
    "olt_type_registry",
]
