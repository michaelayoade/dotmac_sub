"""Legacy network services compatibility module.

Keep imports stable for code still importing from `_legacy` while the service
implementations live in dedicated modules.
"""

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
from app.services.network.splitters import (
    FdhCabinets,
    PonPortSplitterLinks,
    SplitterPortAssignments,
    SplitterPorts,
    Splitters,
    fdh_cabinets,
    pon_port_splitter_links,
    splitter_port_assignments,
    splitter_ports,
    splitters,
)

__all__ = [
    "FdhCabinets",
    "fdh_cabinets",
    "Splitters",
    "splitters",
    "SplitterPorts",
    "splitter_ports",
    "SplitterPortAssignments",
    "splitter_port_assignments",
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
    "PonPortSplitterLinks",
    "pon_port_splitter_links",
]
