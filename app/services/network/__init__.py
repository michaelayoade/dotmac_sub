"""Network services package.

This package provides services for managing network infrastructure including:
- CPE devices and ports
- VLANs
- IP address management
- OLT/PON equipment
- ONT units
- Fiber optic infrastructure (strands, segments, splices, etc.)
- Splitters and FDH cabinets

The package supports both modular imports (from submodules) and legacy imports
from the main network module for backwards compatibility.
"""

# Import from submodules
from app.services.network.cpe import (
    CPEDevices,
    Ports,
    PortVlans,
    Vlans,
    cpe_devices,
    port_vlans,
    ports,
    vlans,
)
from app.services.network.fiber_services import (
    FiberSegments,
    FiberSpliceClosures,
    FiberSplices,
    FiberSpliceTrays,
    FiberStrands,
    FiberTerminationPoints,
    fiber_segments,
    fiber_splice_closures,
    fiber_splice_trays,
    fiber_splices,
    fiber_strands,
    fiber_termination_points,
)
from app.services.network.ip import (
    IPAssignments,
    IpBlocks,
    IpPools,
    IPv4Addresses,
    IPv6Addresses,
    ip_assignments,
    ip_blocks,
    ip_pools,
    ipv4_addresses,
    ipv6_addresses,
)
from app.services.network.olt import (
    OltCardPorts,
    OltCards,
    OLTDevices,
    OltPowerUnits,
    OltSfpModules,
    OltShelves,
    OntAssignments,
    OntUnits,
    PonPorts,
    olt_card_ports,
    olt_cards,
    olt_devices,
    olt_power_units,
    olt_sfp_modules,
    olt_shelves,
    ont_assignments,
    ont_units,
    pon_ports,
)
from app.services.network.ont_actions import (
    OntActions,
    ont_actions,
)
from app.services.network.ont_tr069 import (
    OntTR069,
    ont_tr069,
)
from app.services.network.onu_types import (
    OnuTypes,
    onu_types,
)
from app.services.network.speed_profiles import (
    SpeedProfiles,
    speed_profiles,
)
from app.services.network.splitters import (
    # Splitter services
    FdhCabinets,
    PonPortSplitterLinks,
    # PON port splitter links
    SplitterPortAssignments,
    SplitterPorts,
    Splitters,
    fdh_cabinets,
    pon_port_splitter_links,
    splitter_port_assignments,
    splitter_ports,
    splitters,
)
from app.services.network.zones import (
    NetworkZones,
    network_zones,
)

__all__ = [
    # CPE services
    "CPEDevices",
    "cpe_devices",
    "Ports",
    "ports",
    "Vlans",
    "vlans",
    "PortVlans",
    "port_vlans",
    # IP services
    "IpPools",
    "ip_pools",
    "IpBlocks",
    "ip_blocks",
    "IPv4Addresses",
    "ipv4_addresses",
    "IPv6Addresses",
    "ipv6_addresses",
    "IPAssignments",
    "ip_assignments",
    # OLT services
    "OLTDevices",
    "olt_devices",
    "PonPorts",
    "pon_ports",
    "OntUnits",
    "ont_units",
    "OntAssignments",
    "ont_assignments",
    "OltShelves",
    "olt_shelves",
    "OltCards",
    "olt_cards",
    "OltCardPorts",
    "olt_card_ports",
    "OltPowerUnits",
    "olt_power_units",
    "OltSfpModules",
    "olt_sfp_modules",
    # Splitter services
    "FdhCabinets",
    "fdh_cabinets",
    "Splitters",
    "splitters",
    "SplitterPorts",
    "splitter_ports",
    "SplitterPortAssignments",
    "splitter_port_assignments",
    # Fiber services
    "FiberStrands",
    "fiber_strands",
    "FiberSpliceClosures",
    "fiber_splice_closures",
    "FiberSplices",
    "fiber_splices",
    "FiberSpliceTrays",
    "fiber_splice_trays",
    "FiberTerminationPoints",
    "fiber_termination_points",
    "FiberSegments",
    "fiber_segments",
    # PON port splitter links
    "PonPortSplitterLinks",
    "pon_port_splitter_links",
    # ONT actions
    "OntActions",
    "ont_actions",
    # ONT TR-069
    "OntTR069",
    "ont_tr069",
    # ONU type services
    "OnuTypes",
    "onu_types",
    # Speed profile services
    "SpeedProfiles",
    "speed_profiles",
    # Zone services
    "NetworkZones",
    "network_zones",
]
