"""Fiber network services subpackage.

This module provides services for managing fiber optic network components
including strands, segments, splices, splice closures, trays, and termination points.
"""

# Re-export fiber-related services from the legacy module
from app.services.network._legacy import (
    fiber_strands,
    fiber_segments,
    fiber_splice_closures,
    fiber_splice_trays,
    fiber_splices,
    fiber_termination_points,
    FiberStrands,
    FiberSegments,
    FiberSpliceClosures,
    FiberSpliceTrays,
    FiberSplices,
    FiberTerminationPoints,
)

__all__ = [
    # Service instances
    "fiber_strands",
    "fiber_segments",
    "fiber_splice_closures",
    "fiber_splice_trays",
    "fiber_splices",
    "fiber_termination_points",
    # Classes
    "FiberStrands",
    "FiberSegments",
    "FiberSpliceClosures",
    "FiberSpliceTrays",
    "FiberSplices",
    "FiberTerminationPoints",
]
