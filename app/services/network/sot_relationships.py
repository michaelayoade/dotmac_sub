"""Relationship map for network-domain SOT services.

The dependency direction is intentional:

1. identity: resolves network/customer entities and cross-model links.
2. access_path: resolves customer service path using identity + topology.
3. radius_sessions: resolves online-now and bounded historical NAS evidence.
4. device_state: resolves infrastructure state from poll/live/admin signals.
5. nas_inventory: owns NAS administrative lifecycle state.
6. subscription_nas_assignment: owns commercial-service NAS bindings.
7. nas_lifecycle: composes the owners into guarded reconciliation decisions.
8. nas_access_path_evidence: informs manual lifecycle decisions from history.
9. outage_impact: resolves affected customers from topology/access paths.
10. events: turns state/impact transitions into business events.

Callers should depend on the highest-level service that answers their question
instead of reaching across layers. For example, support/customer surfaces should
ask access_path/outage_impact, not query ONT/NAS/RADIUS tables directly.
"""

from __future__ import annotations

SOT_RELATIONSHIPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity", ()),
    ("access_path", ("identity",)),
    ("radius_sessions", ("identity",)),
    ("device_state", ("identity",)),
    ("nas_inventory", ("identity",)),
    ("subscription_nas_assignment", ("identity",)),
    (
        "nas_lifecycle",
        (
            "identity",
            "access_path",
            "radius_sessions",
            "device_state",
            "nas_inventory",
            "subscription_nas_assignment",
        ),
    ),
    (
        "nas_access_path_evidence",
        ("radius_sessions", "nas_lifecycle"),
    ),
    ("outage_impact", ("access_path", "device_state")),
    ("events", ("device_state", "outage_impact", "radius_sessions")),
)


def dependency_order() -> list[str]:
    return [name for name, _deps in SOT_RELATIONSHIPS]


def dependencies_for(service_name: str) -> tuple[str, ...]:
    for name, dependencies in SOT_RELATIONSHIPS:
        if name == service_name:
            return dependencies
    raise KeyError(service_name)
