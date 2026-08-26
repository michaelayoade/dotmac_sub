"""Shared immutable coordinates for module contracts used across boundaries.

The shadow adoption manifest owns adoption state. A small subset of released
contract coordinates is also needed by production-side parity declarations,
which must not import ``app.shadow``. Keeping those coordinates here gives
both declarations one typed source without making runtime code depend on the
shadow package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ModuleReleaseCoordinates:
    """Exact package, version, and peeled source revision for one release."""

    package: str
    version: str
    revision: str


SUBSCRIPTIONS_RELEASE: Final[ModuleReleaseCoordinates] = ModuleReleaseCoordinates(
    package="dotmac-subscriptions",
    version="0.1.0a3",
    revision="ad6c5824086f6f550447caeabe820e860cdfe23c",
)


__all__ = ["ModuleReleaseCoordinates", "SUBSCRIPTIONS_RELEASE"]
