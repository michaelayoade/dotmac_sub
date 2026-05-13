"""Shared types for the readers subpackage.

``ReadResult`` is parameterised on the observation shape so OLT and ACS
readers each return a typed result without coupling to each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ReadResult(Generic[T]):
    """Outcome of a single OLT or ACS read pass.

    Three meaningful states:

    * ``success=True, unreachable=False`` — clean read. ``observed`` is
      populated; its ``*_present`` field tells you whether the device is
      actually on the surface.
    * ``success=False, unreachable=True`` — couldn't contact the surface at
      all (SSH refused, HTTP 5xx, network timeout). ``observed`` is None.
    * ``success=False, unreachable=False`` — surface responded but the
      reply was unparseable. ``observed`` is None; ``error`` describes why.
    """

    success: bool
    unreachable: bool
    observed: T | None
    error: str | None
