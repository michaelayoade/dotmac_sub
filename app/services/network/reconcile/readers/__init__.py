"""Readers — query OLT and ACS for live state.

Each reader returns a ``ReadResult`` describing one of three outcomes:

* ``success=True, unreachable=False`` — clean read; ``observed`` is populated
  (including the absent-from-surface case, where ``observed`` is non-None but
  its ``*_present`` field is False).
* ``success=False, unreachable=True`` — couldn't contact the surface. The
  precondition layer fast-fails when this happens for a surface the plan
  would write to.
* ``success=False, unreachable=False`` — surface replied with something
  unparseable.

Readers don't retry. The reconciler caller decides retry semantics.
Dependencies are passed in (``OltProtocolAdapter`` / ``GenieACSClient``) so
unit tests substitute stubs. Picking the right adapter/client per ONT is
``reconcile_ont``'s job, not the readers'.
"""

from __future__ import annotations

from ._types import ReadResult
from .acs_reader import read_acs_state
from .olt_reader import read_olt_state

__all__ = ("ReadResult", "read_acs_state", "read_olt_state")
