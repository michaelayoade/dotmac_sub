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
    Vlans,
    PortVlans,
    cpe_devices,
    ports,
    vlans,
    port_vlans,
)
from app.services.network.ip import (
    IpPools,
    IpBlocks,
    IPv4Addresses,
    IPv6Addresses,
    IPAssignments,
    ip_pools,
    ip_blocks,
    ipv4_addresses,
    ipv6_addresses,
    ip_assignments,
)
from app.services.network.olt import (
    OLTDevices,
    PonPorts,
    OntUnits,
    OntAssignments,
    OltShelves,
    OltCards,
    OltCardPorts,
    OltPowerUnits,
    OltSfpModules,
    olt_devices,
    pon_ports,
    ont_units,
    ont_assignments,
    olt_shelves,
    olt_cards,
    olt_card_ports,
    olt_power_units,
    olt_sfp_modules,
)

# Import fiber/splitter services from legacy module
from app.services.network._legacy import (
    # Splitter services
    FdhCabinets,
    Splitters,
    SplitterPorts,
    SplitterPortAssignments,
    fdh_cabinets,
    splitters,
    splitter_ports,
    splitter_port_assignments,
    # Fiber services
    FiberStrands,
    FiberSpliceClosures,
    FiberSplices,
    FiberSpliceTrays,
    FiberTerminationPoints,
    FiberSegments,
    fiber_strands,
    fiber_splice_closures,
    fiber_splices,
    fiber_splice_trays,
    fiber_termination_points,
    fiber_segments,
    # PON port splitter links
    PonPortSplitterLinks,
    pon_port_splitter_links,
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
]
