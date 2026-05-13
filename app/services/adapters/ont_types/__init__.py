"""ONT type adapters registry - transforms only.

Code adapters provide device-specific value transformations.
TR-069 paths are stored in the OnuType database table.

Usage:
    from app.services.adapters.ont_types import ont_type_registry

    # Get adapter by name (from OnuType.adapter_name)
    adapter = ont_type_registry.get("huawei-hg8245h")
    if adapter:
        device_value = adapter.transform_security_mode("WPA2")  # -> "11i"
"""

# Import vendor modules to register adapters
from app.services.adapters.ont_types import huawei  # noqa: F401
from app.services.adapters.ont_types.base import (
    OntTypeAdapter,
    OntTypeRegistry,
    ont_type_registry,
)

__all__ = [
    "OntTypeAdapter",
    "OntTypeRegistry",
    "ont_type_registry",
]
