"""Generic, dependency-free composition engine.

Weighted, tiered layers are matched to a subject, merged deterministically
(NetBox config-context semantics plus tested deviations), validated against
module schemas and compared with observed state. The package is pure: it imports
only the standard library, pydantic and itself, owns no state and does no I/O.
See ``docs/network/ROUTER_CONTROL_PLANE.md`` for the normative specification.
"""
